"""Main entry point for starting sat_executor background server."""

import argparse
from sat_executor.wire import start_server


def main():
    parser = argparse.ArgumentParser(description="SAT Executor Server Process.")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address to bind.")
    parser.add_argument("--port", type=int, default=9876, help="Port to listen on.")
    args = parser.parse_args()

    start_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
