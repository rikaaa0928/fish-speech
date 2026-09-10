class ContextLengthExceededError(ValueError):
    """Raised when a prompt and its requested output cannot fit the model context."""


def resolve_generation_budget(
    max_seq_len: int,
    prompt_tokens: int,
    requested_max_new_tokens: int | None,
) -> int:
    if prompt_tokens >= max_seq_len:
        raise ContextLengthExceededError(
            f"Prompt exceeds the LLAMA context limit: "
            f"prompt={prompt_tokens} tokens, max_seq_len={max_seq_len}."
        )

    effective_max_new_tokens = (
        max_seq_len - prompt_tokens
        if requested_max_new_tokens is None or requested_max_new_tokens == 0
        else requested_max_new_tokens
    )
    if effective_max_new_tokens < 1:
        raise ValueError("max_new_tokens must be greater than 0")
    if prompt_tokens + effective_max_new_tokens > max_seq_len:
        raise ContextLengthExceededError(
            f"Requested sequence exceeds the LLAMA context limit: "
            f"prompt={prompt_tokens} tokens + generation={effective_max_new_tokens} tokens "
            f"= {prompt_tokens + effective_max_new_tokens}, but max_seq_len={max_seq_len}. "
            f"Maximum acceptable generation length here: "
            f"{max_seq_len - prompt_tokens} tokens."
        )
    return effective_max_new_tokens
