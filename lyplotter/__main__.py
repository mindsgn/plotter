"""Launch the LY Drawbot terminal plotter."""

from lyplotter.tui import run


def main() -> None:
    """Entry point for ``python -m lyplotter``.

    Returns:
        None.
    """
    run()


if __name__ == "__main__":
    main()
