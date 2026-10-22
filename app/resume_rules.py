"""Resume rules that ship with the app and can be added to (or refreshed in) a user's master prompt.

The block sits between markers, so the Profile page can add it once and update it in place on later versions
without touching anything else in the prompt. The previous prompt is always kept in profile/history.
"""
from __future__ import annotations

import re

RULES_VERSION = 1
START = "<!-- JOB-AUTOPILOT RESUME RULES"
END = "<!-- /JOB-AUTOPILOT RESUME RULES -->"

BLOCK = f"""{START} v{RULES_VERSION} (added from the dashboard; edit freely, or remove the whole block) -->
SECTION RL — RESUME CONTENT THAT FITS EVERY PROFILE
The app draws the PDF and arranges the sections for the template chosen on Profile → Resume template
(Classic, Projects first, Experience first, or Compact). These rules decide what goes in, so the page is full but never crowded.

RL.1 EMPTY SECTIONS: return an empty list for any section the candidate has no real material for. With no leadership,
awards, publications, or certifications, "leadership" is []; with no projects, "projects" is []. The app then leaves the
section and its heading off the page. Never invent, pad, or repeat items to fill a section.

RL.2 ONE-PAGE BUDGET (A4, at most 670 words). When the profile holds more than fits, choose the most JD-relevant
material and leave the rest out instead of shortening everything:
  * Summary: 2 lines.
  * Experience: the most relevant 2–5 roles, newest first; 3–5 bullets for recent or relevant roles, 1–2 for older ones.
  * Projects: 2–3, with 2–3 bullets each; none for candidates whose experience already fills the page.
  * Technical skills: 3–5 rows, each fitting on one line.
  * Coursework: at most 4 courses per degree, only for students and recent graduates.
  * Leadership & achievements: at most 4 short lines, only real items.

RL.3 SPARSE PROFILES: when there is little to say, fill the page with real detail rather than new sections: an extra bullet
on the most relevant role or project, a fourth course, one more skills row. A short honest resume beats a padded one.

RL.4 ROWS THAT FIT: every entry row (degree | school | location with its dates; company | location · title with its dates;
project | stack with its dates) must fit on one line. Shorten the location to "City, ST", abbreviate long degree names,
and keep a project's stack to its 3–4 most JD-relevant tools.

RL.5 EXPERIENCED CANDIDATES (two or more years of full-time work): lead with experience, keep education to one line per
degree without coursework, and include projects only when they show something the jobs don't.
{END}"""


def status(prompt: str) -> int | None:
    """The rules version already in the prompt, or None."""
    m = re.search(re.escape(START) + r" v(\d+)", prompt or "")
    return int(m.group(1)) if m else None


def apply(prompt: str) -> str:
    """The prompt with the current block added (at the end) or refreshed in place."""
    if status(prompt) is not None:
        return re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: BLOCK, prompt, count=1, flags=re.S)
    return prompt.rstrip() + "\n\n" + BLOCK + "\n"
