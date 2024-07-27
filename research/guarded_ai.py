import yaml
from openai import OpenAI

client = OpenAI()


# Load the YAML activity file
def load_yaml_activity(file_path):
    with open(file_path, "r") as file:
        return yaml.safe_load(file)


# Categorize the user's response using gpt-4o-mini
def categorize_response(question, response, buckets, tokens_for_ai):
    bucket_list = ", ".join(buckets)
    messages = [
        {
            "role": "system",
            "content": f"{tokens_for_ai} Categorize the following response into one of the following buckets: {bucket_list}.",
        },
        {
            "role": "user",
            "content": f"Question: {question}\nResponse: {response}\n\nCategory:",
        },
    ]

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=10,
            temperature=0,
        )
        category = (
            completion.choices[0].message.content.strip().lower().replace(" ", "_")
        )
        return category
    except Exception as e:
        return f"Error: {e}"


# Generate AI feedback using gpt-4o-mini
def generate_ai_feedback(category, question, user_response, tokens_for_ai):
    messages = [
        {
            "role": "system",
            "content": "{tokens_for_ai} Generate a human-readable feedback message based on the following:",
        },
        {
            "role": "user",
            "content": f"Question: {question}\nResponse: {user_response}\nCategory: {category}",
        },
    ]

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, max_tokens=250, temperature=0.7
        )
        feedback = completion.choices[0].message.content.strip()
        return feedback
    except Exception as e:
        return f"Error: {e}"


# Provide feedback based on the category
def provide_feedback(
    yaml_content, section_id, step_id, category, question, user_response
):
    section = next(
        (s for s in yaml_content["sections"] if s["section_id"] == section_id), None
    )
    if not section:
        return "Section not found."

    step = next((s for s in section["steps"] if s["step_id"] == step_id), None)
    if not step:
        return "Step not found."

    transition = step["transitions"].get(category, None)
    if not transition:
        return "Category not found."

    feedback = "\n".join(transition["content_blocks"])
    if "ai_feedback" in transition:
        tokens_for_ai = (
            step["tokens_for_ai"] + " " + transition["ai_feedback"]["tokens_for_ai"]
        )
        ai_feedback = generate_ai_feedback(
            category, question, user_response, tokens_for_ai
        )
        feedback += f"\n\nAI Feedback: {ai_feedback}"

    return feedback


# Simulate the activity
def simulate_activity(yaml_file_path):
    yaml_content = load_yaml_activity(yaml_file_path)
    max_attempts = yaml_content.get("default_max_attempts_per_step", 3)

    for section in yaml_content["sections"]:
        print(f"\nSection: {section['title']}\n")
        for step in section["steps"]:
            # Print all content blocks once per step
            if "content_blocks" in step:
                for block in step["content_blocks"]:
                    print(block)
            if "question" in step:
                question = step["question"]
            else:
                # Skip classification and feedback if there's no question
                continue

            attempts = 0
            while attempts < max_attempts:
                if "question" in step:
                    print(f"\nQuestion: {question}")

                user_response = input("\nYour Response: ")

                category = categorize_response(
                    question, user_response, step["buckets"], step["tokens_for_ai"]
                )
                print(f"\nCategory: {category}")

                feedback = provide_feedback(
                    yaml_content,
                    section["section_id"],
                    step["step_id"],
                    category,
                    question,
                    user_response,
                )
                print(f"\nFeedback: {feedback}")

                if category == "correct":
                    break

                attempts += 1

            if attempts == max_attempts:
                print("\nMaximum attempts reached. Moving to the next step.")


if __name__ == "__main__":
    simulate_activity("activity12.yaml")