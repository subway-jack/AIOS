from cerebrum.manager.agent import AgentManager

import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload agents")
    parser.add_argument(
        "--author",
        required=True
    )
    parser.add_argument(
        "--name",
        required=True
    )
    parser.add_argument(
        "--agenthub_url",
        # default="https://app.aios.foundation"
        default="http://localhost:3000"
    )
    args = parser.parse_args()

    manager = AgentManager(args.agenthub_url)

    agent = manager.download_agent(args.author, args.name)
    print(agent)
    