"""BALAJI terminal mode: python balaji.py"""
from balaji import config, storage, agent
from balaji.ollama_client import OllamaError

MODEL = config.MODEL

def init_db():
    storage.init_db()

def approve_in_terminal(name: str, args: dict) -> bool:
    print(f"\n[Permission needed] {name}: {args}")
    return input("Allow this action? [y/N]: ").strip().lower() in {"y", "yes"}

def ask_balaji(question: str, approval=approve_in_terminal) -> str:
    return agent.ask(question, "terminal", approval)["answer"]

def main() -> None:
    init_db()
    print(f"\nBALAJI — Personal AI Assistant | {MODEL}")
    print("/help | /reset | /exit\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        if not question:
            continue
        if question.lower() in {"/exit", "/quit"}:
            break
        if question.lower() == "/reset":
            storage.clear_history("terminal")
            print("Conversation cleared (saved tasks and memories retained).")
            continue
        if question.lower() == "/help":
            print("Ask questions, request tasks, search memories, or ask about uploaded documents.\nCommands: /reset /exit")
            continue
        try:
            print("BALAJI:", ask_balaji(question), "\n")
        except (OllamaError, ValueError) as exc:
            print("Error:", exc)

if __name__ == "__main__":
    main()
