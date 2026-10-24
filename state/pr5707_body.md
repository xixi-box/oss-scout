HUMAN:

This PR was prepared by my automated coding agent for issue #5649. I have reviewed the diff and the test coverage before opening it, and I'm happy to adjust based on review feedback.

---

AGENT:

## Why

`ConversationManager.createConversation` accepted `maxIterations` and `stuckDetection` but only passed them into the `RemoteConversation` constructor, which never stored them. `start()` then read the values from its own options — which the manager populates with just `initialMessage` and `userId` — so the create payload always carried the server defaults (`max_iterations: 500`, `stuck_detection: true`). A TypeScript consumer using the manager to cap iterations (a cost and runaway limit) or to disable stuck detection silently gets the defaults.

## Summary

- Store `maxIterations` and `stuckDetection` in the `RemoteConversation` constructor, mirroring the existing `hookConfig`/`userId` handling.
- Fall back in `start()` (`options.X ?? this.X ?? default`) so the POST to `/api/conversations` carries the caller's values.
- Regression test in `conversation-scope.test.ts` drives the manager with `{ maxIterations: 7, stuckDetection: false }` and asserts the create payload carries `max_iterations: 7` and `stuck_detection: false`.

## How to Test

From `clients/typescript`:

```sh
npm install
npx vitest run --config vitest.config.ts conversation-scope
```

The new test passes (11/11 in the file); the full TS suite is 329 passed and `tsc --noEmit` is clean.

Fixes #5649
