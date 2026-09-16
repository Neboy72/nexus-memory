/**
 * Test-only module-resolution hook.
 *
 * `tools/search.ts`, `tools/store.ts`, `tools/forget.ts`,
 * `tools/guardrail_check.ts` and `tools/graph_traverse.ts` import
 * `@sinclair/typebox` at runtime, but the package is not installed in a
 * plain checkout. The unit tests only exercise the tool *logic*, so we map
 * the import to a minimal stub whose `Type.*` builders return empty
 * schemas. Register it with `module.register()` before dynamically
 * importing the tool module (see the test files).
 *
 * Strictness (Fund W40): the stub answers only known builder names; a
 * typo like `Type.Ojbect(...)` now throws with a clear message instead of
 * silently returning `{}`. Symbol lookups and `then` return undefined so
 * the namespace can never be mistaken for a thenable. Subpath imports
 * (`@sinclair/typebox/value`, `/compiler`, ...) map to the same stub
 * instead of falling through to ERR_MODULE_NOT_FOUND.
 */
const BUILDERS = [
  "Object", "String", "Number", "Boolean", "Optional", "Array", "Record",
  "Unknown", "Unsafe", "Literal", "Union", "Enum", "Integer", "BigInt",
  "Null", "Undefined", "Never", "Tuple", "Partial", "Required", "Readonly",
  "Intersect", "RegExp", "Date", "Promise", "Ref",
]

const STUB = `
const BUILDERS = new Set([${BUILDERS.map((b) => JSON.stringify(b)).join(",")}]);
export const Type = new Proxy({}, {
  get(_target, prop) {
    if (typeof prop === "symbol") return undefined;
    if (prop === "then") return undefined;
    if (!BUILDERS.has(prop)) {
      throw new Error("Unknown Type." + String(prop) + " — Typo oder fehlt im Typebox-Stub (_typebox-test-loader.mjs)");
    }
    return () => ({});
  },
});
`

export async function resolve(specifier, context, next) {
  if (specifier === "@sinclair/typebox" || specifier.startsWith("@sinclair/typebox/")) {
    return {
      url: "data:text/javascript," + encodeURIComponent(STUB),
      shortCircuit: true,
    }
  }
  return next(specifier, context)
}