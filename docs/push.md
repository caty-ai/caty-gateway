# Push URLs and media

The push helper can ask a connected client to open an HTTP or HTTPS URL. Run it on the gateway host with `CATY_TOKEN` in the process environment; do not place the token on the command line.

```sh
python -m caty_gateway.caty_push open-url 'https://example.com/page' --title 'Example'
python -m caty_gateway.caty_push media 'https://example.com/image.png' --title 'Image'
```

Use single quotes around user-provided URLs and titles. Local files are not accepted. Images may be displayed immediately; video and hosted video links require a client-side tap. Optional flags select a member audience, session, media type, and idempotency key.
