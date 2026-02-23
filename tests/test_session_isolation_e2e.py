"""E2E integration test: verify multi-session window-scoped isolation.

Connects to live Chrome (always-on CDP on port 9222), creates two
sessions, opens tabs in each, and verifies no target overlap.
"""

import asyncio
import sys

sys.path.insert(0, "/var/html/personal/useful-mcp/src")

from wsl_chrome_mcp.chrome_pool import ChromePoolManager


async def main() -> None:
    pool = ChromePoolManager(
        port_min=9222,
        port_max=9322,
        headless=False,
        profile_mode="profile",
        profile_name="Profile 10",
    )

    session_a = "test_isolation_A"
    session_b = "test_isolation_B"
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        status = "PASS" if condition else "FAIL"
        if not condition:
            failed += 1
        else:
            passed += 1
        suffix = f" ({detail})" if detail else ""
        print(f"  [{status}] {name}{suffix}")

    try:
        # --- Step 1: Create two sessions ---
        print("\n=== Step 1: Create sessions ===")
        inst_a = await pool.get_or_create(session_a)
        print(
            f"  Session A created: target={inst_a.current_target_id[:12]}, window={inst_a.window_id}"
        )

        inst_b = await pool.get_or_create(session_b)
        print(
            f"  Session B created: target={inst_b.current_target_id[:12]}, window={inst_b.window_id}"
        )

        check(
            "Sessions have different window IDs",
            inst_a.window_id != inst_b.window_id,
            f"A={inst_a.window_id}, B={inst_b.window_id}",
        )
        check(
            "Initial targets are different",
            inst_a.current_target_id != inst_b.current_target_id,
            f"A={inst_a.current_target_id[:12]}, B={inst_b.current_target_id[:12]}",
        )

        # --- Step 2: Navigate each to different URLs ---
        print("\n=== Step 2: Navigate sessions ===")
        if inst_a.cdp and inst_a.cdp.is_connected:
            await inst_a.cdp.send("Page.navigate", {"url": "https://news.ycombinator.com"})
            await asyncio.sleep(2)
            print("  Session A -> Hacker News")

        if inst_b.cdp and inst_b.cdp.is_connected:
            await inst_b.cdp.send("Page.navigate", {"url": "https://en.wikipedia.org"})
            await asyncio.sleep(2)
            print("  Session B -> Wikipedia")

        # --- Step 3: Create additional tabs in each ---
        print("\n=== Step 3: Create tabs ===")
        tab_a2 = await pool.create_tab(session_a, "https://github.com/trending")
        print(f"  Session A tab 2: {tab_a2[:12]} (GitHub Trending)")
        await asyncio.sleep(2)

        tab_b2 = await pool.create_tab(session_b, "https://www.reddit.com")
        print(f"  Session B tab 2: {tab_b2[:12]} (Reddit)")
        await asyncio.sleep(2)

        print("\n=== Step 4: Verify isolation ===")
        ids_a = set(pool._instances[session_a].targets)
        ids_b = set(pool._instances[session_b].targets)

        print(f"  Session A targets ({len(ids_a)}): {[t[:12] for t in ids_a]}")
        print(f"  Session B targets ({len(ids_b)}): {[t[:12] for t in ids_b]}")

        overlap = ids_a & ids_b
        check("No target overlap between sessions", len(overlap) == 0, f"overlap={overlap}")
        check("Session A has 2 targets", len(ids_a) == 2, f"got {len(ids_a)}")
        check("Session B has 2 targets", len(ids_b) == 2, f"got {len(ids_b)}")

        # Verify window IDs are still correct after tab creation
        inst_a_fresh = pool._instances[session_a]
        inst_b_fresh = pool._instances[session_b]
        check(
            "Window IDs unchanged after tab creation",
            inst_a_fresh.window_id == inst_a.window_id
            and inst_b_fresh.window_id == inst_b.window_id,
            f"A: {inst_a.window_id}->{inst_a_fresh.window_id}, B: {inst_b.window_id}->{inst_b_fresh.window_id}",
        )

        # --- Step 5: Verify new tabs landed in correct windows ---
        print("\n=== Step 5: Verify window placement ===")
        if pool._browser_cdp:
            for label, tid, expected_wid in [
                ("A-tab2", tab_a2, inst_a.window_id),
                ("B-tab2", tab_b2, inst_b.window_id),
            ]:
                try:
                    wr = await pool._browser_cdp.send(
                        "Browser.getWindowForTarget", {"targetId": tid}
                    )
                    actual_wid = wr["windowId"]
                    check(
                        f"{label} in correct window",
                        actual_wid == expected_wid,
                        f"expected={expected_wid}, actual={actual_wid}",
                    )
                except Exception as e:
                    check(f"{label} window check", False, str(e))

        # --- Summary ---
        print(f"\n{'=' * 40}")
        print(f"Results: {passed} passed, {failed} failed")
        print(f"{'=' * 40}")

    finally:
        # Cleanup
        print("\n=== Cleanup ===")
        for sid in [session_a, session_b]:
            try:
                await pool.destroy(sid)
                print(f"  Destroyed {sid}")
            except Exception as e:
                print(f"  Failed to destroy {sid}: {e}")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
