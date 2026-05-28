"""Nano Banana (Gemini 2.5 Flash Image) — edit-only wrapper.

We never generate images from scratch with Nano Banana. Every call must
include an input image; the prompt instructs the model to *edit* that image
(add brand frame, callouts, blur sensitive data, etc.). The system instructions
explicitly forbid synthesizing humans, products, or transactions — this honors
the spec's §16 visual-truthfulness hard guardrail.
"""

import asyncio

import structlog
from google import genai
from google.genai import types

log = structlog.get_logger(__name__)

# Hard-coded model. Bump version deliberately, not via env override.
_MODEL = "gemini-2.5-flash-image"

# Embedded into every prompt. Backstop against synthetic-content generation.
_EDIT_GUARDRAILS = (
    "You are editing an existing screenshot or graphic for PeerMarket, "
    "a Belgian secondhand marketplace whose differentiator is verified-identity trust.\n\n"
    "Hard rules — non-negotiable:\n"
    "- Do NOT synthesize humans, products, or transactions that aren't already in the input image.\n"
    "- Do NOT add stock-photo-style people.\n"
    "- Do NOT fabricate listings or marketplace items.\n"
    "- You MAY: add brand frames (#1a5d3a green), typography overlays, call-out arrows, "
    "  speech-bubble annotations, phone-mockup frames around the input, blur sensitive data, "
    "  apply colour gradients, add the PeerMarket wordmark.\n"
    "- Keep the original image content intact unless explicitly asked to crop or recompose.\n\n"
    "Edit instruction follows:\n"
)


class ImageEditError(RuntimeError):
    """Nano Banana failed (timeout, auth, content policy, parse)."""


class ImageEditDisabled(RuntimeError):
    """No GEMINI_API_KEY configured — image editing is disabled."""


def _build_full_prompt(user_prompt: str) -> str:
    return f"{_EDIT_GUARDRAILS}{user_prompt}"


async def edit_image(
    *,
    api_key: str,
    image_bytes: bytes,
    prompt: str,
    timeout_sec: int = 60,
) -> bytes:
    """Edit `image_bytes` per `prompt`, returning a new PNG.

    See module docstring for the visual-truthfulness contract.
    """
    if not api_key:
        raise ImageEditDisabled(
            "GEMINI_API_KEY is not set; image editing is disabled. "
            "Set the secret + redeploy to enable."
        )

    full_prompt = _build_full_prompt(prompt)
    log.info("nano_banana.start", model=_MODEL, prompt_chars=len(prompt))

    def _sync_call() -> bytes:
        # The google-genai SDK is sync-only; we run it in a thread.
        client = genai.Client(api_key=api_key)
        # Image input + prompt. The SDK accepts a list of parts (text + inline data).
        response = client.models.generate_content(
            model=_MODEL,
            contents=[
                full_prompt,
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            ],
        )
        # The response contains one candidate with parts; we want the inline_data part.
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise ImageEditError("nano_banana returned no candidates")
        parts = candidates[0].content.parts or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data:
                return inline.data
        # If no image came back, the model might have refused (content policy)
        text_part = next((p.text for p in parts if getattr(p, "text", None)), None)
        if text_part:
            raise ImageEditError(f"nano_banana refused to return an image: {text_part[:200]!r}")
        raise ImageEditError("nano_banana response contained no image part")

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=timeout_sec)
    except TimeoutError as e:
        raise ImageEditError(f"nano_banana timeout after {timeout_sec}s") from e
    except ImageEditError:
        raise
    except Exception as e:
        raise ImageEditError(f"nano_banana error: {e}") from e

    log.info("nano_banana.success", bytes_in=len(image_bytes), bytes_out=len(result))
    return result
