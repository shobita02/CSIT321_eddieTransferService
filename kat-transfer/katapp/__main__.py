"""python -m katapp [-c config.toml]: sign in and transfer KAT data until Quit."""

import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m katapp", description=__doc__)
    parser.add_argument("-c", "--config", help="client config (default: config.toml beside the app)")
    args = parser.parse_args(argv)

    from .main import run
    run(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
