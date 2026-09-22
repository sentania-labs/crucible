import os, sys
def main() -> None: os.execvp("crucible", ["crucible", *sys.argv[1:]])
