// Turns the short codes the catalog stores into names a person can read:
// "TH" becomes "Thailand" and "ja" becomes "Japanese". It uses the
// browser's own Intl.DisplayNames, so no country or language list has to
// be kept in the app. A code it does not know, or an environment without
// Intl.DisplayNames, falls back to the code itself, so nothing is ever
// blanked out.

function makeNamer(type) {
  try {
    if (typeof Intl === "undefined" || typeof Intl.DisplayNames !== "function") return null;
    return new Intl.DisplayNames(["en"], { type, fallback: "none" });
  } catch {
    return null;
  }
}

const regionNames = makeNamer("region");
const languageNames = makeNamer("language");

function lookup(namer, value) {
  if (!namer) return null;
  try {
    return namer.of(value) || null;
  } catch {
    return null;
  }
}

export function countryName(code) {
  if (!code) return null;
  const trimmed = String(code).trim();
  if (!trimmed) return null;
  return lookup(regionNames, trimmed.toUpperCase()) ?? trimmed;
}

export function languageName(code) {
  if (!code) return null;
  const trimmed = String(code).trim();
  if (!trimmed) return null;
  return lookup(languageNames, trimmed) ?? trimmed;
}
