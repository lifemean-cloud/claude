"""
자동 실행 프로그램 (GitHub Actions에서 실행됨)

1. skills-lock.json 에 등록된 모든 스킬과, 폴더에 추가된 스킬(.agents/skills, .claude/skills)을
   자동으로 찾아 읽습니다. (Auto-load)
2. 지침 규칙 파일(CLAUDE.md, AGENTS.md, rules/*.md, .claude/rules/*.md 등)도 함께 읽습니다.
3. 작업 지시(tasks/daily_task.md, 또는 원격 실행 때 넘겨준 지시문)를 Claude에게 전달해
   초안을 만들고, 스킬 규칙에 맞춰 한 번 더 검수·개선한 최종본을 받습니다.
4. 결과물을 output/<날짜>/index.html 로 저장하고 output/index.html 아카이브를 갱신합니다.

필요한 것: 환경 변수 ANTHROPIC_API_KEY (GitHub 저장소 Secrets에 등록)
선택: TASK_OVERRIDE (이번 실행에만 쓸 지시문), REFINE_PASSES (검수 횟수, 기본 1)
"""

import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

# ===== 설정 (필요하면 여기만 바꾸세요) =====
ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "skills-lock.json"
# 스킬이 설치될 수 있는 폴더 (앞쪽이 우선)
SKILL_DIRS = [ROOT / ".agents" / "skills", ROOT / ".claude" / "skills"]
# 지침 규칙 파일: 정확한 경로 또는 폴더(그 안의 모든 .md)
RULE_FILES = ["CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md"]
RULE_DIRS = ["rules", ".claude/rules"]
TASK_FILE = ROOT / "tasks" / "daily_task.md"
OUTPUT_DIR = ROOT / "output"

MODEL = "claude-opus-5-5"
EFFORT = "high"
REFINE_PASSES = int(os.environ.get("REFINE_PASSES", "1"))
KST = ZoneInfo("Asia/Seoul")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def find_skill_dir(name: str) -> Path | None:
    for base in SKILL_DIRS:
        candidate = base / name
        if (candidate / "SKILL.md").is_file():
            return candidate
    return None


def discover_skills() -> dict[str, Path]:
    """skills-lock.json 에 등록된 스킬 + 폴더에만 추가된 스킬을 모두 찾습니다."""
    found: dict[str, Path] = {}

    locked = []
    if LOCK_FILE.exists():
        locked = list(json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("skills", {}))
    for name in locked:
        skill_dir = find_skill_dir(name)
        if skill_dir:
            found[name] = skill_dir
        else:
            print(f"[경고] skills-lock.json 의 '{name}' 스킬 파일을 찾지 못해 건너뜁니다.")

    # 잠금 파일에 없어도 폴더에 직접 추가한 스킬까지 포함
    for base in SKILL_DIRS:
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            found.setdefault(skill_md.parent.name, skill_md.parent)

    return found


def load_skills() -> tuple[str, list[str]]:
    """찾은 스킬 폴더의 .md 파일(SKILL.md 먼저)을 하나의 문서로 합칩니다."""
    skills = discover_skills()
    if not skills:
        sys.exit("[오류] 읽을 수 있는 스킬이 하나도 없습니다.")

    parts = []
    for name in sorted(skills):
        skill_dir = skills[name]
        docs = [skill_dir / "SKILL.md"] + sorted(
            p for p in skill_dir.rglob("*.md") if p.name != "SKILL.md"
        )
        body = "\n\n".join(
            f'<file path="{rel(p)}">\n{p.read_text(encoding="utf-8")}\n</file>' for p in docs
        )
        parts.append(f'<skill name="{name}">\n{body}\n</skill>')
    return "\n\n".join(parts), sorted(skills)


def load_rules() -> tuple[str, list[str]]:
    """CLAUDE.md, AGENTS.md, rules/ 폴더 등 지침 규칙 파일을 읽습니다."""
    paths: list[Path] = [ROOT / f for f in RULE_FILES if (ROOT / f).is_file()]
    for d in RULE_DIRS:
        if (ROOT / d).is_dir():
            paths += sorted((ROOT / d).rglob("*.md"))
    text = "\n\n".join(
        f'<rule path="{rel(p)}">\n{p.read_text(encoding="utf-8")}\n</rule>' for p in paths
    )
    return text, [rel(p) for p in paths]


def build_system_prompt(skills_text: str, rules_text: str) -> str:
    rules_block = f"<project_rules>\n{rules_text}\n</project_rules>\n\n" if rules_text else ""
    return (
        "You are a senior front-end designer and engineer. Every skill and project rule below "
        "is loaded for this run; apply them when producing output.\n\n"
        f"{rules_block}"
        f"<design_skills>\n{skills_text}\n</design_skills>\n\n"
        "How to combine the skills:\n"
        "- Project rules come first, then the task, then the skills.\n"
        "- Several skills describe alternative visual directions (e.g. minimalist, brutalist, "
        "high-end, editorial). If the task does not name one, choose the single direction that "
        "fits the task best and apply it consistently; never blend contradictory aesthetics.\n"
        "- Skills about generating images cannot be executed here: apply their art direction "
        "with CSS, SVG and typography instead of image files.\n"
        "- Run each applicable skill's pre-flight / quality checklist before answering.\n\n"
        "Output requirements:\n"
        "- Return ONE complete, self-contained HTML document (inline CSS/JS) and nothing else.\n"
        "- The page is in Korean: set lang=\"ko\" and load a premium Korean-capable font such as "
        "Pretendard from cdn.jsdelivr.net, with sensible fallbacks.\n"
        "- It must look excellent on phones and desktops, and in both light and dark mode.\n"
        "- Do not wrap the document in Markdown code fences."
    )


def ask_claude(client: anthropic.Anthropic, system_prompt: str, user_text: str) -> str:
    """Claude에게 요청하고 HTML 문자열을 돌려받습니다."""
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT},
        # 안전 필터가 거절하면 서버가 알아서 다른 모델로 재시도합니다.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        # 초안·검수 요청이 같은 스킬 규칙을 쓰므로 캐시해서 비용을 줄입니다.
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        reason = response.stop_details.explanation if response.stop_details else "알 수 없음"
        sys.exit(f"[오류] 요청이 거절되었습니다: {reason}")
    if response.stop_reason == "max_tokens":
        sys.exit("[오류] 결과물이 너무 길어 중간에 잘렸습니다. 작업 지시를 줄여 주세요.")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # 혹시 ```html ... ``` 로 감싸서 왔다면 벗겨냅니다.
    fenced = re.search(r"```(?:html)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if "<html" not in text.lower():
        sys.exit("[오류] HTML 문서를 받지 못했습니다.")

    u = response.usage
    print(
        f"[정보] 모델 {response.model} | 입력 {u.input_tokens} · 캐시 쓰기 "
        f"{u.cache_creation_input_tokens or 0} · 캐시 읽기 {u.cache_read_input_tokens or 0} "
        f"· 출력 {u.output_tokens} 토큰"
    )
    return text


def generate_html(client: anthropic.Anthropic, system_prompt: str, task: str, today: str) -> str:
    """초안을 만든 뒤 스킬 체크리스트로 검수해 개선한 최종본을 돌려줍니다."""
    print("[진행] 초안 작성 중...")
    doc = ask_claude(client, system_prompt, f"오늘 날짜: {today}\n\n{task}")

    for i in range(REFINE_PASSES):
        print(f"[진행] 검수·개선 {i + 1}/{REFINE_PASSES} ...")
        doc = ask_claude(
            client,
            system_prompt,
            f"오늘 날짜: {today}\n\n<task>\n{task}\n</task>\n\n"
            f"<draft>\n{doc}\n</draft>\n\n"
            "Review the draft strictly against the task, the project rules and every applicable "
            "skill checklist (layout, typography, spacing, color, motion, accessibility, mobile "
            "and dark mode, Korean copy quality, no placeholders or truncated code, working JS). "
            "Fix every issue you find and return the complete improved HTML document only.",
        )
    return doc


def page_title(doc: str, fallback: str) -> str:
    match = re.search(r"<title>(.*?)</title>", doc, re.IGNORECASE | re.DOTALL)
    return html.unescape(match.group(1).strip()) if match else fallback


def write_archive() -> None:
    """output/index.html 에 날짜별 결과물 목록을 만듭니다."""
    entries = []
    for day_dir in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        page = day_dir / "index.html"
        if day_dir.is_dir() and page.exists():
            title = page_title(page.read_text(encoding="utf-8"), day_dir.name)
            entries.append(
                f'<li><a href="{day_dir.name}/index.html">'
                f'<time>{day_dir.name}</time><span>{html.escape(title)}</span></a></li>'
            )

    archive = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>결과물 아카이브</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
  :root {{ --bg:#f7f6f3; --fg:#141414; --muted:#8a8780; --line:rgba(20,20,20,.08); --hover:#ffffff; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0c0c0d; --fg:#ededec; --muted:#7c7b78; --line:rgba(255,255,255,.08); --hover:#161618; }}
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--fg); font-family:Pretendard,system-ui,sans-serif;
         padding:clamp(48px,10vw,120px) 16px; -webkit-font-smoothing:antialiased; }}
  main {{ max-width:720px; margin:0 auto; }}
  h1 {{ font-size:clamp(32px,6vw,52px); font-weight:600; letter-spacing:-.03em; }}
  p {{ color:var(--muted); margin:12px 0 56px; }}
  ul {{ list-style:none; border-top:1px solid var(--line); }}
  a {{ display:flex; gap:24px; align-items:baseline; padding:20px 12px; color:inherit;
       text-decoration:none; border-bottom:1px solid var(--line); border-radius:12px;
       transition:background .4s cubic-bezier(.16,1,.3,1), transform .4s cubic-bezier(.16,1,.3,1); }}
  a:hover {{ background:var(--hover); transform:translateX(4px); }}
  time {{ color:var(--muted); font-variant-numeric:tabular-nums; font-size:14px; flex-shrink:0; }}
</style>
</head>
<body>
<main>
  <h1>결과물 아카이브</h1>
  <p>자동으로 만들어진 페이지 {len(entries)}개</p>
  <ul>
    {chr(10).join(entries)}
  </ul>
</main>
</body>
</html>
"""
    (OUTPUT_DIR / "index.html").write_text(archive, encoding="utf-8")


def write_summary(lines: list[str]) -> None:
    """GitHub Actions 실행 화면의 Summary 칸에 결과를 보여줍니다."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("[오류] ANTHROPIC_API_KEY 가 없습니다. GitHub 저장소 Secrets에 등록해 주세요.")

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    override = os.environ.get("TASK_OVERRIDE", "").strip()
    task = override or TASK_FILE.read_text(encoding="utf-8")
    task_source = "원격 실행 지시문" if override else rel(TASK_FILE)

    skills_text, skill_names = load_skills()
    rules_text, rule_paths = load_rules()
    print(f"[로드] 스킬 {len(skill_names)}개: {', '.join(skill_names)}")
    print(f"[로드] 규칙 파일 {len(rule_paths)}개: {', '.join(rule_paths) or '없음'}")
    print(f"[로드] 작업 지시: {task_source}")

    system_prompt = build_system_prompt(skills_text, rules_text)
    doc = generate_html(anthropic.Anthropic(), system_prompt, task, today)

    # 같은 날 두 번 이상 실행하면 시각을 붙여 덮어쓰지 않습니다.
    day_dir = OUTPUT_DIR / today
    if (day_dir / "index.html").exists():
        day_dir = OUTPUT_DIR / f"{today}_{now.strftime('%H%M')}"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "index.html").write_text(doc, encoding="utf-8")
    write_archive()

    print(f"[완료] {rel(day_dir)}/index.html 저장")
    write_summary([
        f"## 자동 실행 결과 ({today})",
        f"- 결과물: `{rel(day_dir)}/index.html` — {page_title(doc, today)}",
        f"- 작업 지시: {task_source}",
        f"- 로드한 스킬 ({len(skill_names)}): {', '.join(skill_names)}",
        f"- 로드한 규칙 파일 ({len(rule_paths)}): {', '.join(rule_paths) or '없음'}",
        f"- 검수 횟수: {REFINE_PASSES}",
    ])


if __name__ == "__main__":
    main()
