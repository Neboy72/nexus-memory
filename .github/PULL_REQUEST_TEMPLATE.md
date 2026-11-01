## What was broken

<!-- The behaviour you saw, not the fix. One or two sentences, with the error text or the numbers.
     "It felt wrong" is not a report; "webhooks test produced 8 of 10 subscriptions" is. -->

## Why it happened

<!-- The cause you found. If you did not find it, say so and say what you ruled out. -->

## What this changes

<!-- The smallest change that removes the cause. If a file changed for a second reason, say which. -->

## How you know it works

<!-- The command you ran and its numbers. Not "tests pass": which platform, how many passed,
     how many failed, and what failed before the change. -->

```
python -m pytest tests/ -q
```

## Checklist

- [ ] A test fails before this change and passes after it, and I can name both runs or both outputs
- [ ] No `skip` or `xfail` was added to make a red test green; if a test cannot run on a platform,
      it says why and still runs where it can
- [ ] The behaviour is the same on macOS, Linux and Windows, or the difference is written down
- [ ] `CHANGELOG.md` has an entry that names the defect, not the patch
- [ ] Nothing in this description claims more than the code and the tests show

<!-- New here? The two-commit rule applies to engine changes: one commit with the fix, one commit
     that only moves the version and the pin. See CONTRIBUTING.md. -->
