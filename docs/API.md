# Debugarr HTTP API

Debugarr exposes a JSON API for integrations and a separate Seerr issue
webhook.

- Interactive Swagger UI: `http://<debuggarr-host>:8095/api/docs`
- OpenAPI 3 document: `http://<debuggarr-host>:8095/api/openapi.json`
- Default Seerr webhook: `http://<debuggarr-host>:8095/debuggarr/seerr-webhook`

The HTTP port defaults to `8095` and can be changed with
`SEERR_WEBHOOK_PORT`. The webhook path can be changed with
`SEERR_WEBHOOK_PATH`; the OpenAPI document uses the configured path.

## Authentication

Set `API_SECRET` to enable the REST API. Send its raw value in either header:

```http
Authorization: <API_SECRET>
```

or:

```http
X-API-Key: <API_SECRET>
```

Do not add a `Bearer` prefix. Requests to `/api/docs` and `/api/openapi.json`
are public so the documentation can be opened without credentials; operations
shown in Swagger UI still require the API secret. If `API_SECRET` is unset,
REST operations return `503`. Missing or incorrect credentials return `403`.

The Seerr webhook uses `SEERR_WEBHOOK_SECRET` when configured. Its raw value
may be sent in `Authorization` or `X-Webhook-Secret`. If that setting is empty,
the webhook does not require a secret; restrict access to a trusted network.

## REST endpoints

All `/api/*` operations below require the API secret.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/bugs` | List bug reports; optional `status` query filter |
| `GET` | `/api/bugs/{bug_id}` | Get one bug |
| `GET` | `/api/bugs/{bug_id}/bundle` | List diagnostic bundle files |
| `GET` | `/api/bugs/{bug_id}/bundle/{filename}` | Read a bundle text file |
| `POST` | `/api/bugs/{bug_id}/fixed` | Mark a bug fixed; optional JSON `description` |
| `POST` | `/api/bugs/{bug_id}/reopen` | Reopen a bug; optional JSON `reason` |
| `POST` | `/api/bugs/{bug_id}/edit` | Replace a bug's `description` |
| `DELETE` | `/api/bugs/{bug_id}` | Remove a bug and its messages |
| `POST` | `/api/notify` | Send a private administrator notification |
| `POST` | `/api/notify/{source}/{notification_id}/resolve` | Remove and resolve a notification |
| `POST` | `/api/reviews` | Create an integration decision request |
| `GET` | `/api/reviews?source={source}&status=decided` | Read integration decisions |
| `POST` | `/api/reviews/{review_id}/complete` | Complete a decided integration review |
| `POST` | `/api/reviews/{review_id}/withdraw` | Withdraw an unanswered integration review |
| `POST` | `/api/mediacheck/reviews` | Create a Mediacheck replacement review |
| `GET` | `/api/mediacheck/reviews` | Read confirmed or denied Mediacheck decisions |
| `POST` | `/api/mediacheck/reviews/{review_id}/complete` | Complete a decided Mediacheck review |

Error responses are JSON objects with an `error` field. Common status codes
are `400` for invalid input, `403` for failed authentication, `404` for an
unknown ID, `409` for an invalid state transition, `502` for a failed Discord
DM operation, and `503` when `API_SECRET` is not configured.

## Examples

List open bugs:

```sh
curl -H "X-API-Key: $API_SECRET" \
  "http://localhost:8095/api/bugs?status=open"
```

Send a tracked notification:

```sh
curl -X POST http://localhost:8095/api/notify \
  -H "Authorization: $API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "mediacheck",
    "notification_id": "job-123",
    "message": "A media check needs attention."
  }'
```

Resolve it after the condition clears:

```sh
curl -X POST \
  http://localhost:8095/api/notify/mediacheck/job-123/resolve \
  -H "X-API-Key: $API_SECRET"
```

Create an integration review:

```sh
curl -X POST http://localhost:8095/api/reviews \
  -H "X-API-Key: $API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "mediacheck",
    "review_id": "MC-12AB34CD",
    "title": "Confirm media identity",
    "summary": "Check the proposed replacement before continuing.",
    "details": {"path": "/media/example.mkv"},
    "actions": [
      {"command": "replace", "description": "Replace the file"},
      {"command": "accept", "description": "Keep the current file"}
    ]
  }'
```

The review ID is unique within Debugarr. Keep it stable when retrying a request
so clients can safely recover from a timeout. Integration decisions can be
queried with `GET /api/reviews?source=mediacheck&status=decided`; after handling
one, call the corresponding `/complete` endpoint with a non-empty `result`.
Use `/withdraw` only while the administrator has not decided yet.

## Seerr webhook

Configure Seerr to send issue notifications to the configured webhook path.
When `SEERR_WEBHOOK_SECRET` is set, include it as the raw `Authorization` or
`X-Webhook-Secret` header value. The endpoint accepts Seerr's JSON issue
payload and returns an accepted/ignored status; it is not an `/api/*` endpoint
and uses the webhook secret rather than `API_SECRET`.

## Operational security

Keep both secrets private. Do not expose the API or webhook directly to the
public internet; use a trusted network, firewall, or authenticated reverse
proxy. Diagnostic bundle endpoints may contain private logs and configuration
details. Access to Debugarr's Docker socket can grant effective host control,
so keep its container allow-list narrow.
