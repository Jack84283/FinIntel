import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = readFileSync(join(import.meta.dirname, "DatabaseBackupCard.tsx"), "utf8");

describe("database backup restore controls", () => {
  it("enables the Restore button on selection without awaiting preview", () => {
    expect(source).toContain('disabled={busy || !selected || confirming} onClick={openRestore}');
    expect(source).not.toContain('disabled={busy || !preview || confirming}');
  });

  it("requires inspected backup metadata before Merge or Replace can write data", () => {
    expect(source).toContain('disabled={busy || inspecting || !preview} onClick={() => { void restore("merge"); }}');
    expect(source).toContain('disabled={busy || inspecting || !preview || replacePhrase !== "REPLACE"}');
    expect(source).toContain("preview.id !== selected");
  });

  it("rejects stale preview responses after a different backup is selected", () => {
    expect(source).toContain("requestId === inspectionSequence.current");
    expect(source).toContain("++inspectionSequence.current; // Ignore the result of any previous selection.");
  });
});
