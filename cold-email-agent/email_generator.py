import json
import re
import requests

import config


def _call_groq(prompt):
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
        "max_tokens": 800,
    }
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _build_prompt(lead):
    name    = lead.get("name", "there")
    company = lead.get("company", "your company")

    extra_fields = {
        k: v for k, v in lead.items()
        if k not in ("name", "company", "email") and v
    }
    extra_context = ""
    if extra_fields:
        lines = [f"  - {k}: {v}" for k, v in extra_fields.items()]
        extra_context = "Additional context about this lead:\n" + "\n".join(lines) + "\n"

    industry = extra_fields.get("industry", "their field")

    prompt = f"""
You are {config.SENDER_NAME}, {config.SENDER_ROLE} at {config.SENDER_COMPANY}.

Write a cold outreach email to {name} at {company}.

What we do:
{config.SENDER_VALUE_PROP}

{extra_context}
Rules:
- Write 200-250 words.
- Open with a very specific observation about {company} — something that shows you actually looked them up. Reference their industry, their specific pain point, or something unique about their positioning.
- Do NOT open with their name. Start with the observation.
- Use their exact pain point naturally in the email — don't be generic about it.
- Make them feel like this email was written only for them and no one else.
- Tell a mini story or use an analogy relevant to their specific industry — {industry}.
- Reveal the solution only after building curiosity.
- End with one soft CTA like "Worth a quick call this week?"
- Sound like a human founder, not a marketer.
- No cliches, no "Hope this finds you well", no "I came across your profile".
- Do NOT add a signature block.

IMPORTANT: Return ONLY a JSON object on a single line with no line breaks inside string values. Use \\n for line breaks. Example:
{{"subject": "Your subject here", "body": "Line one.\\nLine two.\\nLine three."}}
""".strip()

    return prompt


def generate_email(lead):
    prompt = _build_prompt(lead)
    raw    = _call_groq(prompt)

    cleaned = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    cleaned = re.sub(r'(?<!\\)\n\s*(?=")', '', cleaned)
    cleaned = re.sub(r'\n', '\\n', cleaned)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        subject_match = re.search(r'"subject"\s*:\s*"([^"]+)"', cleaned)
        body_match    = re.search(r'"body"\s*:\s*"(.*?)"(?:\s*})', cleaned, re.DOTALL)
        if subject_match and body_match:
            result = {
                "subject": subject_match.group(1),
                "body": body_match.group(1).replace('\\n', '\n')
            }
        else:
            raise ValueError(f"Model returned invalid JSON.\nRaw output:\n{raw}")

    if "subject" not in result or "body" not in result:
        raise ValueError(f"Missing subject or body in: {result}")

    return result


if __name__ == "__main__":
    sample_lead = {
        "name":    "Dr. Kiran Naik",
        "company": "The Beauty Doctors",
        "email":   "kiran.plastic@gmail.com",
        "industry": "Cosmetic & Plastic Surgery",
        "pain_point": "Multi-location clinic struggling to maintain consistent patient flow",
    }
    print("Generating email...")
    email = generate_email(sample_lead)
    print("\nSUBJECT:", email["subject"])
    print("\nBODY:\n",  email["body"])