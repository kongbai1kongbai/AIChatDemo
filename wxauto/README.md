# WeChat Auto Reply (wxautoz)

This directory is reserved for the personal WeChat auto-reply bot.

The main program is at `../auto_reply.py` (root of code/).

## Usage

```bash
# From project root (D:\AI\code)
python auto_reply.py --remote gemini-3.5-flash

# Or use the bat script
auto-reply.bat
```

## Note

The existing `auto_reply.py` at the root level uses wxautoz for WeChat UI automation.
It includes its own inline AI engine and search logic.

The WeCom (Enterprise WeChat) bot in `../wecom/` uses the extracted shared modules
in `../shared/` for a cleaner architecture.
