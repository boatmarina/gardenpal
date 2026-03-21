import argparse
import sys

from gardenpal import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="gardenpal",
        description="GardenPal - your personal garden assistant",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gardenpal {__version__}",
    )

    args = parser.parse_args()


if __name__ == "__main__":
    main()
