/**
 * Test-only module-resolution hook.
 *
 * `tools/forget.ts` and `tools/guardrail_check.ts` import
 * `@sinclair/typebox` at runtime, but the package is not installed in a
 * plain checkout. The unit tests only exercise the tool *logic*, so we map
 * the import to a minimal stub whose `Type.*` builders return empty
 * schemas. Register it with `module.register()` before dynamically
 * importing the tool module (see the test files).
 */
const STUB = `
export const Type = new Proxy({}, { get: () => () => ({}) });
export default { Type };
`

export async function resolve(specifier, context, next) {
  if (specifier === "@sinclair/typebox") {
    return {
      url: "data:text/javascript," + encodeURIComponent(STUB),
      shortCircuit: true,
    }
  }
  return next(specifier, context)
}
