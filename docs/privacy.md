# Privacy

The core gateway processes audio, messages, attachments, and pairing data on the host. When history is enabled, conversation text is stored under the configured `CATY_HISTORY_DIR`; operators should protect that directory and remove it to delete retained local history.

Optional avatar generation sends source image bytes, style-reference image bytes, and generation prompts to the configured Poyo and Renoise services. Optional vision description sends image bytes and a description prompt to Anthropic. Provider-side retention and training policies are controlled by those providers and the account terms selected by the operator. Review those terms before enabling either feature.

Provider credentials are read from environment variables and must not be logged. Missing required credentials or avatar style configuration leaves the corresponding route disabled with an HTTP 503 response; this fail-closed behavior does not replace the disclosure above.
