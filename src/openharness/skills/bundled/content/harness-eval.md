---
name: harness-eval
description: This skill should be used when the user asks to "test the harness", "run integration tests", "validate features with real API", "test with real model calls", "run agent loop tests", or needs to verify OpenHarness features end-to-end on a real codebase with actual LLM calls.
---

# Harness Eval — End-to-End Feature Validation with Real Agent Loops

Validate OpenHarness features by running real agent loops against an unfamiliar codebase with actual LLM API calls. This is not unit testing — every test exercises the full stack: API client → model → tool calls → execution → result.

## When to Use

- After implementing or modifying a feature (swarm, hooks, skills, memory, etc.)
- After merging external PRs that touch core modules
- Before releasing a new version
- When verifying compatibility with a new LLM provider

## Core Principles

1. **Test on an unfamiliar project** — never test on OpenHarness itself (the agent would be modifying its own code). Clone a real project like `https://github.com/HKUDS/AutoAgent` as the workspace.
2. **Use real API calls** — no mocks. Configure a real LLM endpoint (Anthropic, OpenAI-compatible, or any supported provider).
3. **Multi-turn conversations** — single-turn tests miss context bugs. Always test 2+ turns where the model needs prior context.
4. **Combine features** — test hooks+skills+agent loop together, not in isolation. Real usage always combines features.
5. **Verify tool execution** — check that tools actually ran (not just that the model mentioned them). Inspect tool call lists and output files.

## Test Design Pattern

Each test follows this structure:

```python
async def test_feature_on_real_project():
    # 1. Set up engine with real API client pointing to workspace
    engine = make_engine(system_prompt="...", cwd=UNFAMILIAR_PROJECT)

    # 2. Run multi-turn agent loop with tool-heavy prompts
    evs1 = [ev async for ev in engine.submit_message("Read X, analyze Y")]
    r1 = collect(evs1)  # collect text, tools, turns, tokens

    # 3. Follow-up turn testing context retention
    evs2 = [ev async for ev in engine.submit_message("Based on what you found...")]
    r2 = collect(evs2)

    # 4. Verify: tools were called, output is correct, files exist
    assert "grep" in r1["tools"]
    assert len(r2["text"]) > 100
```

## Feature Test Matrix

### Engine & Tools
- **Multi-turn memory**: Set a fact in turn 1, ask about it in turn 3
- **Tool chaining**: glob → grep → read in one task
- **Write→Edit→Read**: Create file, modify it, verify content
- **Parallel tools**: Model issues 3+ tool calls in one response
- **Error recovery**: Model encounters tool error, adapts approach
- **Auto-compaction**: Run 5+ tasks on shared engine, verify no context overflow

### Swarm & Coordinator
- **InProcessBackend lifecycle**: spawn → active → status → shutdown
- **Concurrent teammates**: 2+ in-process agents running simultaneously
- **Coordinator + TaskNotification**: Multi-turn delegation with XML notifications
- **Permission sync**: request → pending → resolve → verify

### Hooks, Skills, Plugins
- **Hook blocks tool → model adapts**: pre_tool_use hook blocks bash, model switches to glob
- **Skill tool invocation**: Model calls `skill("name")`, gets content, follows instructions
- **Plugin skill loading**: Plugin provides skill, model uses it through skill tool
- **Hook + skill combined**: Hook gates file writes, skill guides the workflow

### Memory, Session, Config
- **Memory with frontmatter**: Save .md files with YAML frontmatter, search by body content
- **Session save → resume**: Multi-turn conversation saved, loaded into new engine, context preserved
- **Cost tracking**: Tokens accumulate correctly across turns
- **Cron CRUD**: Create jobs, toggle, mark_run, delete, validate expressions

### Provider Compatibility
- **Anthropic client**: Standard tool calling flow
- **OpenAI client**: Tool calling + reasoning_content handling for thinking models
- **Multi-turn via OpenAI**: Verify reasoning_content round-trip across turns

## Running the Eval

```bash
# Set up workspace (clone once)
git clone https://github.com/HKUDS/AutoAgent /tmp/eval-workspace

# Run all eval tests
python tests/test_merged_prs_on_autoagent.py
python tests/test_real_large_tasks.py
python tests/test_hooks_skills_plugins_real.py

# Or run the quick unit tests (no API needed)
python -m pytest tests/ -q -k "not autoagent"
```

## Environment Variables

```bash
ANTHROPIC_API_KEY=sk-xxx          # Required
ANTHROPIC_BASE_URL=https://...    # Anthropic-compatible endpoint
OPENAI_BASE_URL=https://...       # OpenAI-compatible endpoint (for PR #14 tests)
ANTHROPIC_MODEL=kimi-k2.5         # Model to use
```

## Interpreting Results

- **PASS with tool calls**: Feature works end-to-end
- **PASS without tool calls**: Model answered from knowledge, didn't exercise the feature — rewrite prompt to force tool use
- **FAIL with exception**: Code bug — read traceback
- **FAIL with wrong output**: Model behavior issue — check system prompt and tool schemas
- **Timeout**: Increase `max_turns` or simplify the task prompt

## Common Pitfalls

- Testing on OpenHarness itself — agent modifies its own code
- Using mocks instead of real API — misses serialization bugs
- Single-turn only — misses context accumulation bugs
- Not checking tool call list — model may claim it used a tool without actually calling it
- Hardcoding paths — use `WORKSPACE` variable, skip tests in CI with `pytest.mark.skipif`
