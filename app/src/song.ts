const GENERIC = new Set("a an the my our own some any good nice great cool epic fun happy sad upbeat calm chill slow fast lofi background royalty free trending this that".split(" "));
/** A song or artist the request names ("concerts with BUNT best day of my life song", "edit it to ben bohmer's songs"), or a link. */
export function songIn(prompt: string): string {
  const link = prompt.match(/https?:\/\/\S+/);
  if (link && /\b(?:songs?|tracks?|music)\b/i.test(prompt)) return link[0];  // other links are style references
  const m = prompt.match(/\b(?:to|with|using|over|on)\s+(.+?)(?:'s|’s)?\s+(?:songs?|tracks?|music)\b/i);
  if (!m) return "";
  const name = m[1].split(/\b(?:to|with|using|over|on)\s+/i).pop()!.trim();  // "japan to ben bohmer" -> "ben bohmer"
  return name && !name.toLowerCase().split(/\s+/).every((w) => GENERIC.has(w)) ? name : "";
}
