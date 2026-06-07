import os

from agent.gmail_client import register_watch

TOPIC = os.getenv("PUBSUB_TOPIC_NAME", "projects/email-agent-ozzy/topics/gmail-push")

if __name__ == "__main__":
    result = register_watch(TOPIC)
    print(f"Watch renewed — historyId={result['historyId']}, expiration={result['expiration']}")
