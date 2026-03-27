import argparse

from gardenpal import __version__
from gardenpal.web import create_app


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
    parser.add_argument(
        "command",
        nargs="?",
        default="help",
        choices=["help", "serve"],
        help="Run 'serve' to launch the web app.",
    )

    args = parser.parse_args()
    if args.command == "serve":
        app = create_app()
        app.run(debug=True)


if __name__ == "__main__":
    main()
