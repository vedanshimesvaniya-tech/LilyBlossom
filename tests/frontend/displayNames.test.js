import { describe, expect, it } from "vitest";
import { countryName, languageName } from "../../src/lib/displayNames.js";

describe("countryName", () => {
  it("turns a country code into its English name", () => {
    expect(countryName("TH")).toBe("Thailand");
    expect(countryName("KR")).toBe("South Korea");
    expect(countryName("JP")).toBe("Japan");
  });

  it("accepts lowercase codes and surrounding spaces", () => {
    expect(countryName(" th ")).toBe("Thailand");
  });

  it("returns null for a missing or blank code", () => {
    expect(countryName(null)).toBeNull();
    expect(countryName(undefined)).toBeNull();
    expect(countryName("   ")).toBeNull();
  });

  it("keeps a code it does not know instead of blanking it", () => {
    expect(countryName("ZZZZ")).toBe("ZZZZ");
  });
});

describe("languageName", () => {
  it("turns a language code into its English name", () => {
    expect(languageName("ja")).toBe("Japanese");
    expect(languageName("th")).toBe("Thai");
    expect(languageName("ko")).toBe("Korean");
  });

  it("returns null for a missing or blank code", () => {
    expect(languageName(null)).toBeNull();
    expect(languageName("")).toBeNull();
  });
});
