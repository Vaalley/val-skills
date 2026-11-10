# val-skills

Small, opinionated skills for AI-assisted software engineering. Plain Markdown instructions, not another agent framework.

## Install

Requires Git and [Node.js 22.20+](https://nodejs.org/). Run from the project where you want to use a skill:

```sh
npx skills add Vaalley/val-skills --skill evil-twin
npx skills add Vaalley/val-skills --skill cold-start
npx skills add Vaalley/val-skills --skill jira-browser-cookie-auth
```

The [skills CLI](https://github.com/vercel-labs/skills) lets you choose the coding agents to install to. Add `-g` for a user-level installation across projects, or omit `--skill` to select interactively.

Choose a specific agent without prompts:

```sh
# Claude Code
npx skills add Vaalley/val-skills --skill <skill-name> --agent claude-code -y

# Codex
npx skills add Vaalley/val-skills --skill <skill-name> --agent codex -y
```

List available skills in the collection:

```sh
npx skills add Vaalley/val-skills --list
```

The installer is third-party software. Inspect skills before enabling them; they guide an agent with your existing tool permissions, not an enforced sandbox.

### omp

For a native user-level omp installation, clone this repository and copy the skills you want into `~/.omp/agent/skills/` (or copy `val-skills/skills/*` to install all of them). No Node.js required for this route.

macOS / Linux / Git Bash:

```sh
git clone https://github.com/Vaalley/val-skills.git
mkdir -p ~/.omp/agent/skills
cp -R val-skills/skills/evil-twin val-skills/skills/cold-start ~/.omp/agent/skills/
```

PowerShell:

```powershell
git clone https://github.com/Vaalley/val-skills.git
New-Item -ItemType Directory -Force "$HOME/.omp/agent/skills" | Out-Null
Copy-Item -Recurse -Force ./val-skills/skills/evil-twin, ./val-skills/skills/cold-start "$HOME/.omp/agent/skills/"
```

These commands copy the current version. To update a copied skill, pull the repository and repeat the copy step. For other agents, you can likewise copy an individual skill folder into their documented skills directory.

## Skills

Start a fresh agent session after installation so the skill can be discovered. Invocation syntax depends on your agent (`/skill-name` in Claude Code, `$skill-name` in Codex, `/skill:skill-name` in omp), or you can ask your agent to apply the skill by name.

### [evil-twin](skills/evil-twin/SKILL.md)

**Every feature deserves a nemesis.**

Build checkout; meet the Impatient Buyer who submits twice. Build autosave; meet the Fast Typist on Slow Wi-Fi whose older save finishes last.

Evil Twin identifies your feature's promise, invents a relevant adversary, and runs a few bounded attacks against real behavior. It brings back observed failures and reproductions, not a list of hypothetical code-review concerns.

- Starts with an ordinary successful interaction to separate bugs from broken setup.
- Leaves implementation unchanged by default.
- Uses synthetic data and disposable local targets; checks for live backing services before exercising side effects.
- Reports limitations honestly when execution is unavailable or nothing breaks.

#### Use it

```text
Claude Code: /evil-twin the checkout flow
Codex:       $evil-twin the checkout flow
omp:         /skill:evil-twin the checkout flow
```

Without a target, the skill uses your current task and related uncommitted changes. You can also ask your agent to use the `evil-twin` skill on a named feature.

The report identifies the promise, demonstrated failure, reproduction steps, evidence, other attacks attempted, and execution limits. No finding is a valid outcome; it is not a guarantee of correctness or security.

### [cold-start](skills/cold-start/SKILL.md)

**Can a new contributor get this running with only what the project tells them?**

Cold Start follows the documented setup in a disposable workspace, then exercises one meaningful first use. It reports observed blockers, hidden prerequisites, and workarounds that actually worked—not speculative documentation improvements.

- Distinguishes a clean checkout from a fresh machine and records inherited tooling.
- Leaves the source project unchanged; installs dependencies and tries bounded workarounds only in the disposable environment.
- Checks backing services before startup, migrations, or seeds; does not borrow personal credentials or touch live systems.
- Separates success on the documented path from success requiring undocumented steps.

#### Use it

```text
Claude Code: /cold-start this repository
Codex:       $cold-start this repository
omp:         /skill:cold-start this repository
```

Without a target, the skill uses the current project and its documented default onboarding path. Name a component or setup guide to narrow a monorepo run.

The report records the starting environment, reproduction steps, observed first success or blocker, verified deviations, and cleanup. Tests or a running process alone do not prove the first-use workflow works.

### [jira-browser-cookie-auth](skills/jira-browser-cookie-auth/SKILL.md)

**Read a Jira Cloud ticket, its comments and its attachments from your logged-in browser, without an API token.**

A bundled Python reader reuses the Jira session of a local Brave, Chrome or Chromium profile on Linux to fetch an issue, every comment, and download its attached mockups and files. Built for agents that need the full ticket, including the screenshot "below".

- GET-only, with an exact endpoint allowlist: the issue, its comments, and the attachments listed by that issue. Redirects are never followed.
- Attachments are streamed from the Jira tenant itself (`redirect=false`), so cookies never reach another host; files are size-limited, sanitized, `0600`, never overwritten.
- Never prints or stores keyring secrets or decrypted cookies; reads the cookie database through a read-only SQLite snapshot.
- Requires Python 3.9+ with `requests` and `cryptography`, and `secret-tool` for the browser keyring. Offline tests: `python3 -m unittest discover -s scripts -p 'test_read_issue.py'`.

#### Use it

```text
Claude Code: /jira-browser-cookie-auth https://example.atlassian.net/browse/PROJ-123
Codex:       $jira-browser-cookie-auth https://example.atlassian.net/browse/PROJ-123
omp:         /skill:jira-browser-cookie-auth https://example.atlassian.net/browse/PROJ-123
```

The ticket text and attachments are treated as untrusted data, never as instructions. The skill never writes to Jira, even when a ticket asks for it.

## Add a skill

Each skill lives one level below `skills/`:

```text
skills/
  cold-start/
    SKILL.md
  evil-twin/
    SKILL.md
  your-next-skill/
    SKILL.md
```

1. Create `skills/<name>/SKILL.md` with YAML frontmatter containing `name` and `description`. Use a lowercase, hyphenated name matching its directory.
2. Write self-contained instructions: when to use it, what it should do, its permission boundaries, and what evidence it should return. Include supporting files only when the skill needs them.
3. Exercise it on a disposable target, document it here, and check discovery with `npx skills add . --list`.

See the [Agent Skills specification](https://agentskills.io/specification) for the portable format. No root-level `SKILL.md` or custom installer is needed; new skill folders are discovered automatically by the skills CLI.

## License

[MIT](LICENSE).
