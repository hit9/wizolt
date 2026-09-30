# Compatibility catalog

wizolt recognizes common providers and models so you can usually connect with just a URL,
key and model name. It chooses the API protocol and supported options for you.
Unknown endpoints still work through the ordinary OpenAI-compatible API.

## Check your settings

Run **`/config`** to see which protocol and reasoning options wizolt is using.
Run **`/catalog`** to see the compatibility data's version and last update.

## Updates

wizolt checks for updates in the background at most once every **72 hours**. An update takes
effect in your next session. Run **`/catalog sync`** to check and apply an update now.
If the check fails, your current data stays in use.

## Override a choice

Your explicit settings take precedence. If your endpoint needs something different, adjust
its [provider settings](configuration.md#optional-provider-settings):

| What needs changing | Setting |
| --- | --- |
| API protocol | `api` |
| Reasoning format or history | `chat_reasoning`, `reasoning_history` |
| Available reasoning levels | `[provider.NAME.models]` |
| Extra or rejected request fields | `extra_body`, `omit_body` |

Use the endpoint's own documentation to choose the value, then check `/config` again.
