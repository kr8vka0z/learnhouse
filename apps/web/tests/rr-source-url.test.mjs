// Rescue Ready fork: the AGPL "Source code" link URL comes from an env var and
// lands in an href, so only http(s) URLs may pass; anything else falls back.
import { describe, expect, test } from "bun:test";

import { DEFAULT_SOURCE_CODE_URL, resolveSourceCodeUrl } from "../lib/rr/branding.ts";

describe("resolveSourceCodeUrl", () => {
  test("unset or blank uses the default repository", () => {
    expect(resolveSourceCodeUrl(undefined)).toBe(DEFAULT_SOURCE_CODE_URL);
    expect(resolveSourceCodeUrl("  ")).toBe(DEFAULT_SOURCE_CODE_URL);
  });

  test("an http(s) URL is used as given", () => {
    expect(resolveSourceCodeUrl("https://git.example.org/lms")).toBe("https://git.example.org/lms");
  });

  test("a non-http scheme can never become the link", () => {
    expect(resolveSourceCodeUrl("javascript:alert(1)")).toBe(DEFAULT_SOURCE_CODE_URL);
    expect(resolveSourceCodeUrl("//evil.example")).toBe(DEFAULT_SOURCE_CODE_URL);
  });
});
