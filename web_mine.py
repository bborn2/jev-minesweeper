"""
Web Minesweeper player.
Reads the game state from https://minesweeperonline.com/ and asks the Jev model
(https://docs.typesafe.ai/) which hidden cells are safe and which are mines.
"""

import argparse
import os
import random

from dotenv import load_dotenv
from typesafe_sdk import Choice, TypeSafeClient
from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import UnexpectedAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from webdriver_manager.microsoft import EdgeChromiumDriverManager
import numpy as np
import time
from typing import Dict, List, Tuple, Optional


# Command-line difficulty argument -> difficulty name on the website
DIFFICULTY_LEVELS = {1: "beginner", 2: "intermediate", 3: "expert"}

# Only send the remaining mine count to Jev once this few cells are left hidden (endgame)
MINE_COUNT_MAX_CELLS = 60

# Maximum questions per request; larger sets are split into batches
MAX_QUESTIONS = 150
# Cells with a safe probability >= this threshold are clicked
SAFE_THRESHOLD = 0.9
# Cells are flagged only at or above this mine probability; a wrong flag corrupts later
# deductions, so the threshold is kept high
MINE_THRESHOLD = 0.9

# The state holds only the rules: the full board is mostly irrelevant noise for a single
# judgment, and the Jev docs recommend sending only what the question needs
MINESWEEPER_RULES = (
    "Minesweeper. Cells are written as (row,col). Each question gives a group of hidden "
    "cells and exactly how many mines are among them. If that number is 0, every cell "
    "in the group is safe. If that number equals the number of cells in the group, "
    "every cell is a mine. Otherwise the group alone does not tell which cells are mines."
)


class JevAdvisor:
    """Splits the board into clue groups and asks Jev whether each hidden cell is safe or a mine"""

    def __init__(self, model: Optional[str] = None):
        """
        Args:
            model: Jev model name; defaults to TYPESAFE_DEFAULT_MODEL, then jev-latest
        """
        load_dotenv()
        if not os.getenv("TYPESAFE_API_KEY", "").strip():
            raise RuntimeError("TYPESAFE_API_KEY is not set; add it to .env")
        # The SDK reads TYPESAFE_API_KEY from the environment
        self.client = TypeSafeClient(model=model)

    @staticmethod
    def neighbors(board: List[List[int]], row: int, col: int):
        """Yield the coordinates of the 8 cells around (row, col)"""
        rows, cols = len(board), len(board[0])
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                r, c = row + dr, col + dc
                if 0 <= r < rows and 0 <= c < cols:
                    yield r, c

    def frontier_cells(self, board: List[List[int]]) -> List[Tuple[int, int]]:
        """Hidden cells next to a number; only these have clues to judge from"""
        cells = []
        for r, row in enumerate(board):
            for c, value in enumerate(row):
                if value != -1:
                    continue
                if any(1 <= board[nr][nc] <= 8 for nr, nc in self.neighbors(board, r, c)):
                    cells.append((r, c))
        return cells

    def clue_groups(self, board: List[List[int]],
                    mines_left: Optional[int] = None) -> List[dict]:
        """
        Turn the clues into facts of the form "this group of hidden cells holds exactly N mines".
        Counting is done in code because Jev is weak at arithmetic.

        Besides one fact per number cell, "subset difference" facts are added: when all hidden
        cells of clue A are also hidden cells of clue B, the cells only B has hold exactly
        (mines B still needs - mines A still needs) mines. This turns a two-step deduction
        into a single fact.
        When mines_left is given, global facts based on the remaining mine count are added too.
        """
        base = []
        for r, row in enumerate(board):
            for c, value in enumerate(row):
                if not 1 <= value <= 8:
                    continue
                hidden = frozenset(
                    (nr, nc) for nr, nc in self.neighbors(board, r, c) if board[nr][nc] == -1
                )
                if not hidden:
                    continue
                flags = sum(board[nr][nc] == 10 for nr, nc in self.neighbors(board, r, c))
                base.append({"source": f"clue ({r},{c}) showing {value}",
                             "cells": hidden, "mines": value - flags})

        groups = {}
        for g in base:
            groups.setdefault((g["cells"], g["mines"]), g)
        for a in base:
            for b in base:
                if a is b or not a["cells"] < b["cells"]:
                    continue
                rest = b["cells"] - a["cells"]
                key = (rest, b["mines"] - a["mines"])
                groups.setdefault(key, {
                    "source": f"{b['source']} minus the cells it shares with {a['source']}",
                    "cells": rest, "mines": key[1],
                })

        # "Partial overlap" facts (the classic 1-2 pattern): when A and B share cells but
        # neither contains the other, the shared cells hold at most A's mines, so B's own
        # cells hold at least (B's mines - A's mines). If that equals the number of B's own
        # cells, they are all mines, the shared cells hold exactly A's mines, and A's own
        # cells are all safe. Only emitted when an exact count follows, to keep the
        # "exactly N mines" format.
        for a in base:
            for b in base:
                shared = a["cells"] & b["cells"]
                a_only, b_only = a["cells"] - b["cells"], b["cells"] - a["cells"]
                if a is b or not shared or not a_only or not b_only:
                    continue
                if b["mines"] - a["mines"] != len(b_only):
                    continue
                pair = f"{b['source']} compared with {a['source']}"
                groups.setdefault((b_only, len(b_only)),
                                  {"source": pair, "cells": b_only, "mines": len(b_only)})
                groups.setdefault((a_only, 0),
                                  {"source": pair, "cells": a_only, "mines": 0})

        if mines_left is not None:
            self.add_mine_count_facts(board, mines_left, groups)
        return list(groups.values())

    @staticmethod
    def add_mine_count_facts(board: List[List[int]], mines_left: int, groups: dict):
        """
        Add "global" facts from the remaining mine count; most useful in the endgame.

        1. All hidden cells together hold exactly mines_left mines.
        2. Pick several non-overlapping clue groups with known mine counts and subtract their
           cells and mines from the global fact: the remaining cells hold exactly
           (mines_left - sum of those groups' mines). For example, if the frontier already
           uses up every remaining mine, all cells away from the numbers are safe.
        """
        hidden = frozenset((r, c) for r, row in enumerate(board)
                           for c, v in enumerate(row) if v == -1)
        # Early in the game there are too many hidden cells; global facts decide nothing
        # and only make the request longer
        if not hidden or len(hidden) > MINE_COUNT_MAX_CELLS:
            return
        source = f"mine counter: {mines_left} mines left on the board"
        groups.setdefault((hidden, mines_left),
                          {"source": source, "cells": hidden, "mines": mines_left})

        # Different pick orders give different non-overlapping combinations; try both to
        # cover more endgame positions
        known = [g for g in groups.values() if g["cells"] != hidden]
        orders = [
            sorted(known, key=lambda g: -len(g["cells"])),
            sorted(known, key=lambda g: len(g["cells"])),
        ]
        for order in orders:
            used, mines, picked = frozenset(), 0, []
            for g in order:
                if used.isdisjoint(g["cells"]):
                    used |= g["cells"]
                    mines += g["mines"]
                    picked.append(g)
            rest = hidden - used
            if len(picked) < 2 or not rest:
                continue  # Subtracting a single group is covered by the loop below
            groups.setdefault((rest, mines_left - mines), {
                "source": f"{source}, minus {len(picked)} non-overlapping clue groups "
                          f"holding {mines} mines",
                "cells": rest, "mines": mines_left - mines,
            })

        # Global fact minus a single clue group (subset difference)
        for g in known:
            rest = hidden - g["cells"]
            if rest:
                groups.setdefault((rest, mines_left - g["mines"]), {
                    "source": f"{source}, minus {g['source']}",
                    "cells": rest, "mines": mines_left - g["mines"],
                })

    def guess_risk(self, board: List[List[int]],
                   mines_left: Optional[int]) -> Dict[Tuple[int, int], float]:
        """
        When nothing is certain and a guess is needed, estimate the risk of each hidden cell
        (plain counting, since Jev is weak at numeric comparison).

        Frontier cells: the highest "mines / cells" ratio among the clue groups they belong to.
        Cells away from numbers: the remaining mines, minus roughly what the frontier uses,
        spread evenly over those cells.
        """
        risk: Dict[Tuple[int, int], float] = {}
        for g in self.clue_groups(board):
            density = g["mines"] / len(g["cells"])
            for cell in g["cells"]:
                risk[cell] = max(risk.get(cell, 0.0), density)

        interior = [(r, c) for r, row in enumerate(board) for c, v in enumerate(row)
                    if v == -1 and (r, c) not in risk]
        if interior:
            if mines_left is None:
                density = 0.5  # Without a mine count, do not prefer distant cells
            else:
                density = max(0.0, mines_left - sum(risk.values())) / len(interior)
            for cell in interior:
                risk[cell] = min(density, 1.0)
        return risk

    def analyze(self, board: List[List[int]],
                mines_left: Optional[int] = None) -> Dict[Tuple[int, int], Tuple[float, float]]:
        """
        Ask Jev one Choice per clue group: all safe / all mines / undetermined.
        Each question carries a single fact and no full board.

        Args:
            mines_left: remaining mine count from the page counter, used for endgame facts

        Returns:
            {(row, col): (safe probability, mine probability)}, the maximum over the
            groups containing the cell
        """
        groups = self.clue_groups(board, mines_left)
        if not groups:
            return {}

        questions = {}
        for i, g in enumerate(groups):
            cells = sorted(g["cells"])
            questions[f"g{i}"] = Choice(
                # source is only for logging; its "showing N" differs from the remaining
                # mine count and would confuse Jev
                instructions={
                    "hidden_cells": [f"({r},{c})" for r, c in cells],
                    "number_of_hidden_cells": len(cells),
                    "mines_among_hidden_cells": g["mines"],
                    "question": "Following `rules`, what does this fact say about `hidden_cells`?",
                },
                criteria={
                    "all_safe": "mines_among_hidden_cells is 0, so every hidden cell is safe",
                    "all_mines": "mines_among_hidden_cells equals number_of_hidden_cells, "
                                 "so every hidden cell is a mine",
                    "undetermined": "Some but not all hidden cells are mines; "
                                    "this fact alone cannot tell which",
                },
            )

        # Send in batches to stay within the per-request context limit
        state = {"rules": MINESWEEPER_RULES}
        answers = {}
        ids = list(questions)
        for start in range(0, len(ids), MAX_QUESTIONS):
            batch = {k: questions[k] for k in ids[start:start + MAX_QUESTIONS]}
            response = self.client.system_one(state=state, questions=batch)
            answers.update(response.choices)
            print(f"Jev({response.model}) judged {len(batch)} facts, "
                  f"input_tokens={response.usage.input_tokens}")

        # Print every question of this round and Jev's answer
        print(f"Rules (state.rules): {MINESWEEPER_RULES}")
        for i, g in enumerate(groups):
            ans = answers[f"g{i}"]
            cells = " ".join(f"({r},{c})" for r, c in sorted(g["cells"]))
            probs = " ".join(f"{k}={v:.2f}" for k, v in ans.probabilities.items())
            print(f"  [g{i}] Q: hidden cells {cells} ({len(g['cells'])} cells) hold exactly "
                  f"{g['mines']} mines  (source: {g['source']})")
            print(f"        A: {ans.choice} (confidence {ans.confidence:.2f})  {probs}")

        result: Dict[Tuple[int, int], Tuple[float, float]] = {}
        for i, g in enumerate(groups):
            probs = answers[f"g{i}"].probabilities
            for cell in g["cells"]:
                safe, mine = result.get(cell, (0.0, 0.0))
                result[cell] = (max(safe, probs["all_safe"]), max(mine, probs["all_mines"]))
        return result

    def close(self):
        self.client.close()


class MinesweeperWebReader:
    """Reads and plays the web Minesweeper game"""

    def __init__(self, debug_mode: bool = True):
        """
        Initialize the reader

        Args:
            debug_mode: show the browser window and keep debugging information
        """
        self.driver = None
        self.debug_mode = debug_mode
        self.board = []
        self.rows = 0
        self.cols = 0

    def start_browser(self):
        """Start the Edge browser"""
        edge_options = Options()

        if self.debug_mode:
            # Debug mode settings
            edge_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            edge_options.add_experimental_option('useAutomationExtension', False)
            edge_options.add_argument("--start-maximized")
            edge_options.add_argument("--disable-blink-features=AutomationControlled")

        # Keep the browser window open after the script exits
        edge_options.add_experimental_option("detach", True)
        edge_options.add_argument("--disable-gpu")
        edge_options.add_argument("--no-sandbox")
        edge_options.add_argument("--disable-dev-shm-usage")

        # Download and use a matching EdgeDriver automatically
        service = Service(EdgeChromiumDriverManager().install())
        self.driver = webdriver.Edge(service=service, options=edge_options)
        print("Edge browser started (debug mode)")

    def open_game(self, url: str = "https://minesweeperonline.com/"):
        """
        Open the Minesweeper page

        Args:
            url: game URL
        """
        if not self.driver:
            self.start_browser()

        print(f"Opening: {url}")
        self.driver.get(url)

        # Wait for the game to load
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "game"))
            )
            print("Game page loaded")
            time.sleep(1)  # Extra wait to make sure loading is complete
        except Exception as e:
            raise RuntimeError("Failed waiting for the game to load") from e

    def select_difficulty(self, level: str = "beginner"):
        """
        Select the difficulty

        Args:
            level: 'beginner' (9x9), 'intermediate' (16x16), 'expert' (16x30)
        """
        if level not in ("beginner", "intermediate", "expert"):
            print(f"Unknown difficulty {level}, keeping the current one")
            return
        try:
            # Click "Game" at the top to open the options dialog
            self.driver.find_element(By.ID, "options-link").click()
            WebDriverWait(self.driver, 5).until(
                EC.visibility_of_element_located((By.ID, "options"))
            )
            # The difficulty radio button's id is the difficulty name
            self.driver.find_element(By.ID, level).click()
            # The "New Game" button submits the form and rebuilds the board
            self.driver.find_element(By.CSS_SELECTOR, "#options-form input[type=submit]").click()
            WebDriverWait(self.driver, 5).until(
                EC.invisibility_of_element_located((By.ID, "options"))
            )
            time.sleep(1)
            print(f"Difficulty selected: {level}")
        except Exception as e:
            raise RuntimeError(f"Failed to select difficulty {level}") from e

    def parse_cell_class(self, class_name: str) -> int:
        """
        Parse a cell's CSS class into its state

        Args:
            class_name: the cell's class attribute

        Returns:
            Cell state: -1 (hidden), 0 (empty), 1-8 (number), 9 (mine), 10 (flag)
        """
        if not class_name:
            return -1

        classes = class_name.split()

        # Hidden cell
        if 'blank' in classes:
            return -1

        # Flag
        if 'bombflagged' in classes or 'flagged' in classes:
            return 10

        # Opened cell: look for an openN class
        for i in range(9):
            if f'open{i}' in classes:
                return i  # 0 is empty, 1-8 are numbers

        # Mines
        if 'bombdeath' in classes or 'bombrevealed' in classes:
            return 9

        # Question mark or any unrecognized state
        return -1

    def read_board(self) -> List[List[int]]:
        """
        Read the current board

        Returns:
            The board as a 2D list with values:
            -1: hidden
            0: empty
            1-8: number
            9: mine
            10: flag
        """
        try:
            # Cell ids are "row_col" (1-based). The page also renders a ring of hidden
            # display:none border cells; they must be excluded, or the row count is wrong
            # and non-interactable hidden cells get clicked.
            # One JS call fetches every visible cell's id and class, much faster than
            # calling get_attribute per cell
            cells = self.driver.execute_script("""
                return Array.from(document.querySelectorAll('#game div.square'))
                    .filter(el => el.style.display !== 'none')
                    .map(el => [el.id, el.className]);
            """)

            if not cells:
                raise RuntimeError("No cell elements found")

            states = {}
            for cell_id, class_name in cells:
                r, c = (int(x) for x in cell_id.split("_"))
                states[(r - 1, c - 1)] = self.parse_cell_class(class_name)

            rows = max(r for r, _ in states) + 1
            cols = max(c for _, c in states) + 1

            print(f"Board size: {rows} rows x {cols} cols")

            self.rows = rows
            self.cols = cols

            # Build the board from the parsed cells
            board = [[states.get((r, c), -1) for c in range(cols)] for r in range(rows)]
            open_count = sum(v != -1 for v in states.values())  # Debug: count opened cells

            print(f"Debug: {open_count} opened cells read")

            self.board = board
            return board

        except Exception as e:
            raise RuntimeError("Failed to read the board") from e

    def print_board(self):
        """Print the board"""
        if not self.board:
            print("Board is empty")
            return

        print("\nBoard (-1: hidden, 0: empty, 1-8: number, 9: mine, 10: flag):")
        print("=" * (self.cols * 4))
        for row in self.board:
            print(' '.join(f"{cell:3}" for cell in row))
        print("=" * (self.cols * 4))

    def get_board_array(self) -> np.ndarray:
        """
        Get the board as a numpy array

        Returns:
            numpy array
        """
        return np.array(self.board)

    def click_cell(self, row: int, col: int, right_click: bool = False):
        """
        Click a cell

        Args:
            row: row index (0-based)
            col: column index (0-based)
            right_click: right click (place a flag)
        """
        try:
            if not (0 <= row < self.rows and 0 <= col < self.cols):
                print(f"Cell index out of range: [{row}, {col}]")
                return

            # Cell ids on the page are 1-based: "row_col"
            cell_id = f"{row + 1}_{col + 1}"
            cell = self.driver.find_element(By.ID, cell_id)
            before_class = cell.get_attribute("class")

            if right_click:
                # Right click (flag); duration=0 removes ActionChains' default 250ms pointer move
                ActionChains(self.driver, duration=0).context_click(cell).perform()
            else:
                cell.click()

            # The page updates the DOM synchronously in its click handler, so check right away
            if cell.get_attribute("class") == before_class:
                print(f"Warning: cell [{row}, {col}] did not change after the click")

        except UnexpectedAlertPresentException:
            # After a win the page opens a prompt asking for a name to submit the score
            print("Game won; the page opened the score submission prompt")
        except Exception as e:
            raise RuntimeError(f"Failed to click cell [{row}, {col}]") from e

    def game_status(self) -> str:
        """
        Determine the game status from the face button

        Returns:
            'playing', 'won' or 'lost'
        """
        try:
            face_class = self.driver.find_element(By.ID, "face").get_attribute("class") or ""
            if "facewin" in face_class:
                return "won"
            if "facedead" in face_class:
                return "lost"
        except Exception:
            raise RuntimeError("Unable to determine game status")
        # Fallback: a mine on the board means the game was lost
        if any(9 in row for row in self.board):
            return "lost"
        return "playing"

    def mines_left(self) -> Optional[int]:
        """Read the remaining-mines counter (total mines - flags); digit classes look like time9"""
        try:
            digits = self.driver.execute_script("""
                return ['mines_hundreds', 'mines_tens', 'mines_ones']
                    .map(id => document.getElementById(id).className);
            """)
            if any(d == "time-" for d in digits):
                return None  # A minus sign means more flags than mines, so a flag is wrong
            return int("".join(d.replace("time", "") for d in digits))
        except Exception:
            return None

    def auto_play(self, advisor: JevAdvisor, max_steps: int = 200) -> str:
        """
        Loop: read the board -> ask Jev -> flag mines / click every safe cell -> update the board

        Args:
            advisor: the Jev advisor
            max_steps: maximum number of rounds

        Returns:
            Final game status, or 'max_steps_reached' if the limit is exhausted
        """
        for step in range(1, max_steps + 1):
            board = self.read_board()
            status = self.game_status()
            if status != "playing":
                return status

            unknown = [(r, c) for r in range(self.rows) for c in range(self.cols)
                       if board[r][c] == -1]
            if not unknown:
                return self.game_status()

            print(f"\n=== Round {step} ===")
            mines_left = self.mines_left()
            probs = advisor.analyze(board, mines_left)

            if not probs:
                # No number clues yet (opening move): click the center, else a random hidden cell
                target = (self.rows // 2, self.cols // 2)
                if target not in unknown:
                    target = random.choice(unknown)
                print(f"No clues available, clicking {target}")
                self.click_cell(*target)
                continue

            # A cell judged both safe and a mine is contradictory; act on neither
            mines = [cell for cell, (s, m) in probs.items()
                     if m >= MINE_THRESHOLD and s < SAFE_THRESHOLD]
            safes = [cell for cell, (s, m) in probs.items()
                     if s >= SAFE_THRESHOLD and m < MINE_THRESHOLD]

            # Cells Jev judges to be mines: flag them and update the map
            for r, c in mines:
                print(f"Jev: ({r},{c}) is a mine (probability {probs[(r, c)][1]:.2f}), flagging")
                self.click_cell(r, c, right_click=True)

            # Cells Jev judges safe: click all of them
            for r, c in safes:
                # An earlier click may already have opened this cell; only click hidden ones
                cls = self.driver.find_element(By.ID, f"{r + 1}_{c + 1}").get_attribute("class")
                if "blank" not in cls:
                    continue
                print(f"Jev: ({r},{c}) is safe (probability {probs[(r, c)][0]:.2f}), clicking")
                self.click_cell(r, c)
                if self.game_status() != "playing":
                    return self.game_status()

            if not mines and not safes:
                # Nothing is certain, so guess: pick the lowest-risk cell by mine density
                risk = advisor.guess_risk(board, mines_left)
                lowest = min(risk.values())
                target = random.choice([cell for cell, p in risk.items() if p == lowest])
                where = "frontier" if target in probs else "distant"
                print(f"No certain cells, guessing {where} cell {target} "
                      f"(estimated risk {lowest:.2f})")
                self.click_cell(*target)

        return "max_steps_reached"

    def new_game(self):
        """Start a new game"""
        try:
            # Click the face button to restart
            face = self.driver.find_element(By.ID, "face")
            face.click()
            print("New game started")
            time.sleep(0.5)
        except Exception as e:
            print(f"Failed to start a new game: {e}")

    def close(self):
        """Close the browser"""
        if self.driver:
            self.driver.quit()
            print("Browser closed")


def parse_args() -> str:
    """Command-line argument: 1 beginner / 2 intermediate / 3 expert, default 2"""
    parser = argparse.ArgumentParser(
        description="Play Minesweeper on minesweeperonline.com automatically with Jev")
    parser.add_argument(
        "level", nargs="?", type=int, choices=sorted(DIFFICULTY_LEVELS), default=2,
        help="difficulty: 1=beginner 9x9 (10 mines), 2=intermediate 16x16 (40 mines), "
             "3=expert 16x30 (99 mines); default 2",
    )
    return DIFFICULTY_LEVELS[parser.parse_args().level]


def main():
    """Play Minesweeper automatically with Jev"""
    level = parse_args()
    # Create the advisor first so a missing token fails before the browser opens
    advisor = JevAdvisor()
    reader = MinesweeperWebReader(debug_mode=True)

    try:
        # Open the game
        reader.open_game()

        # Wait for the page to finish loading
        time.sleep(3)

        # Pick the difficulty given on the command line from the Game menu
        reader.select_difficulty(level)

        # Let Jev judge and click
        result = reader.auto_play(advisor)
        print(f"\nGame over: {result}")

        print("\n=== Final board ===")
        board = reader.read_board()
        reader.print_board()

        # Statistics
        board_array = np.array(board)
        unique, counts = np.unique(board_array, return_counts=True)
        print("\nStatistics:")
        names = {-1: "hidden", 0: "empty", 9: "mine", 10: "flag"}
        for val, count in zip(unique, counts):
            print(f"  {names.get(val, f'number {val}')}: {count}")

        # Leave the browser open: start_browser sets detach, so the window survives exit
        print("\nThe browser stays open; close it manually")

    finally:
        advisor.close()


if __name__ == "__main__":
    main()
