// Provider payloads use logical paths. Host paths remain private to the SDK.
export function providerView(value: unknown, roots: readonly string[]): unknown {
  if (typeof value === "string") {
    let text = value;
    for (const root of roots) text = text.replaceAll(root, ".");
    return text;
  }
  if (Array.isArray(value)) return value.map((item) => providerView(item, roots));
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, providerView(item, roots)]),
    );
  return value;
}
