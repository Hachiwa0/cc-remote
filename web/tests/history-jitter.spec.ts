import { expect, test } from "@playwright/test";

/**
 * Visible-jitter measurement for pull-to-load-older. Run explicitly:
 *
 *     npm --prefix web run test:jitter
 *
 * This is an explicitly-run WebKit interaction gate, deliberately kept out of
 * `test:reliability` and `test:history-browser`. It measures every intermediate
 * frame and enforces the accepted jump/blind-frame limits; endpoint-only tests
 * cannot catch a page which moves during the drag and lands correctly later.
 *
 * Metric: per frame, take every row present in BOTH this frame and the
 * previous one, and use the median change in its VIEWPORT offset. That median
 * is what the reader's eye registers as "the text moved".
 *
 * Deliberately NOT used (each was tried and is wrong):
 *  - a row's document position: legitimately moves when rows above re-measure.
 *  - |offset delta + scrollTop delta|: on a prepend scrollTop MUST change by
 *    the inserted height, precisely so the content holds still.
 *  - a single tracked row: it scrolls off-screen and reports a fake huge jump.
 *  - viewport-only row tracking: with 2-3 rows mounted on an iPhone the set can
 *    turn over completely across a legitimate correction, and such a "blind"
 *    frame cannot distinguish holding the reader still from teleporting them.
 *
 * Fidelity limit: Playwright's WebKit cannot drive a native touch drag, so the
 * gesture below is synthetic events plus explicit scrollTop writes. It has no
 * rubber-banding and no momentum, and the product also writes scrollTop, so the
 * two writers interleave in an order a real iPhone would not produce. Treat a
 * reproducible signature as real and a one-off as suspect.
 */
async function startTracking(page: import("@playwright/test").Page) {
  await page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    if (!viewport) throw new Error("no thread");
    const store = (window as unknown as {
      __samples: {
        scrollTop: number; rows: Record<string, number>;
        mark: string; height: number; tf: string;
        lead: string; total: string; count: string;
      }[];
    });
    store.__samples = [];
    const sample = () => {
      const viewportRect = viewport.getBoundingClientRect();
      const rows: Record<string, number> = {};
      // Track a band well beyond the viewport. With only 2-3 rows mounted on an
      // iPhone viewport the visible set can turn over completely across a
      // legitimate prepend compensation, and a "no shared rows" frame cannot
      // distinguish holding the reader still from teleporting them.
      const margin = 4000;
      for (const row of document.querySelectorAll<HTMLElement>("[data-turn-id]")) {
        const rect = row.getBoundingClientRect();
        if (rect.bottom <= viewportRect.top - margin
          || rect.top >= viewportRect.bottom + margin) continue;
        rows[row.dataset.turnId!] = rect.top - viewportRect.top;
      }
      const sizer = viewport.querySelector<HTMLElement>(".virtual-thread-in");
      store.__samples.push({
        scrollTop: viewport.scrollTop,
        rows,
        mark: (window as unknown as { __mark: string }).__mark ?? "",
        height: viewport.scrollHeight,
        tf: sizer?.style.transform || "-",
        lead: sizer?.dataset.lead ?? "-",
        total: sizer?.dataset.total ?? "-",
        count: sizer?.dataset.count ?? "-",
      });
      (window as unknown as { __frame: number }).__frame =
        requestAnimationFrame(sample);
    };
    sample();
  });
}

async function stopTracking(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    cancelAnimationFrame((window as unknown as { __frame: number }).__frame);
    const samples = (window as unknown as {
      __samples: {
        scrollTop: number; rows: Record<string, number>;
        mark: string; height: number; tf: string;
        lead: string; total: string; count: string;
      }[];
    }).__samples;
    const motion: number[] = [];
    let blind = 0;
    let travel = 0;
    for (let index = 1; index < samples.length; index += 1) {
      const previous = samples[index - 1];
      const current = samples[index];
      travel += Math.abs(current.scrollTop - previous.scrollTop);
      const shifts: number[] = [];
      for (const id of Object.keys(current.rows)) {
        if (previous.rows[id] == null) continue;
        shifts.push(current.rows[id] - previous.rows[id]);
      }
      if (!shifts.length) {
        blind += 1;
        motion.push(Math.abs(current.scrollTop - previous.scrollTop));
        continue;
      }
      shifts.sort((a, b) => a - b);
      motion.push(Math.abs(shifts[Math.floor(shifts.length / 2)]));
    }
    const JUMP = 120;
    const detail: string[] = [];
    motion.forEach((value, index) => {
      if (value <= JUMP) return;
      const window_ = samples.slice(Math.max(0, index - 2), index + 4)
        .map((s) => {
          const rows = Object.entries(s.rows)
            .sort((left, right) => left[1] - right[1])
            .slice(0, 4)
            .map(([id, offset]) => `${id}@${Math.round(offset)}`)
            .join(",");
          return `${s.mark}|top=${Math.round(s.scrollTop)}|h=${s.height}`
            + `|lead=${s.lead}|total=${s.total}|count=${s.count}`
            + `|rows=${rows}|tf=${s.tf}`;
        });
      detail.push(`JUMP ${Math.round(value)}px at frame ${index + 1}: ` + window_.join("  >>  "));
    });
    return {
      frames: samples.length,
      jumps: motion.filter((value) => value > JUMP).length,
      worst: Number(Math.max(0, ...motion).toFixed(1)),
      blind,
      travel: Math.round(travel),
      detail,
    };
  });
}

async function touch(
  page: import("@playwright/test").Page,
  type: "touchstart" | "touchmove" | "touchend",
  clientY: number,
) {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const point = { identifier: 1, target, clientX: 120, clientY: input.clientY };
    const event = new Event(input.type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: input.type === "touchend" ? [] : [point] },
      targetTouches: { value: input.type === "touchend" ? [] : [point] },
      changedTouches: { value: [point] },
    });
    target.dispatchEvent(event);
  }, { type, clientY });
}

/** One physical pull: finger down, drag toward history driving real scrollTop,
 * finger up. Mirrors what iOS does natively, which Playwright cannot drive. */
async function mark(page: import("@playwright/test").Page, label: string) {
  await page.evaluate((value) => {
    (window as unknown as { __mark: string }).__mark = value;
  }, label);
}

async function pull(page: import("@playwright/test").Page, steps: number) {
  const viewport = page.locator(".thread");
  await mark(page, "down");
  await touch(page, "touchstart", 120);
  for (let index = 1; index <= steps; index += 1) {
    await touch(page, "touchmove", 120 + index * 40);
    await viewport.evaluate((node) => {
      node.scrollTop = Math.max(0, node.scrollTop - 40);
      node.dispatchEvent(new Event("scroll"));
    });
    await mark(page, `mv${index}`);
    await page.waitForTimeout(16);
  }
  await mark(page, "UP");
  await touch(page, "touchend", 120 + steps * 40);
  await page.waitForTimeout(260);
  await mark(page, "idle");
}

test("repeated pulls to the top", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "webkit", "mobile gesture");
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  // Isolate page-window commits here. Delayed image/Markdown row growth has a
  // separate exact keyed-anchor regression in history-browser.spec.ts; mixing
  // it into this median-band instrument reports expected offscreen motion as a
  // visible page jump.
  await page.goto(
    "/tests/history-browser.html?pages=4&delay=120&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await page.waitForTimeout(300);

  await startTracking(page);
  for (let round = 0; round < 4; round += 1) {
    await pull(page, 8);
    await page.waitForTimeout(400);
  }
  const result = await stopTracking(page);
  console.log(`PULLS frames=${result.frames} jumps=${result.jumps} worst=${result.worst} blind=${result.blind} travel=${result.travel}`);
  for (const line of result.detail) console.log("  " + line);
  expect(result.frames).toBeGreaterThan(0);
  expect(result.blind).toBe(0);
  expect(result.jumps).toBe(0);
  expect(result.worst).toBeLessThanOrEqual(80);
  expect(pageErrors).toEqual([]);
});
