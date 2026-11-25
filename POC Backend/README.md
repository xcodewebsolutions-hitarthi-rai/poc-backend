# Frontend Login API

A deployment-ready Node.js/Express API for a frontend-only website. It uses configurable static demo credentials, JWT bearer tokens, exact-origin CORS, and Swagger UI. No database is required.

## Requirements and local setup

- Node.js 20+
- npm

```powershell
npm install
Copy-Item .env.example .env
```

Edit `.env`: set a private random `JWT_SECRET` of at least 32 characters, change the demo password, and set `FRONTEND_ORIGINS` to the exact comma-separated frontend origins, including scheme and port. Then start the API:

```powershell
npm run dev
```

- Swagger UI: <http://localhost:8000/docs>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- Health: <http://localhost:8000/health>

The sample credentials are `demo@example.com` / `ChangeMe123!`. Change these before exposing the service publicly.

## Endpoints

### `POST /api/v1/auth/login`

```json
{
  "email": "demo@example.com",
  "password": "ChangeMe123!"
}
```

On success (`200`):

```json
{
  "access_token": "<JWT>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": { "email": "demo@example.com" }
}
```

Invalid credentials return `401`; invalid input returns `400`.

### `GET /api/v1/auth/me`

Requires `Authorization: Bearer <access_token>`. Returns the authenticated user's email or `401` if the token is missing, invalid, or expired. Use Swagger UI's **Authorize** button to test it.

## Frontend integration

Set `FRONTEND_ORIGINS` to your deployed site's origin. The frontend can display login errors and navigate to the dashboard only after a successful login:

```javascript
async function login(email, password) {
  const response = await fetch("https://YOUR_API_HOST/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const result = await response.json();

  if (!response.ok) {
    throw new Error(result.detail || "Unable to log in");
  }

  sessionStorage.setItem("access_token", result.access_token);
  window.location.assign("/dashboard");
}

// In the form's submit handler:
try {
  await login(email, password);
} catch (error) {
  showError(error.message);
}
```

Send the token for protected calls:

```javascript
const response = await fetch("https://YOUR_API_HOST/api/v1/auth/me", {
  headers: {
    Authorization: `Bearer ${sessionStorage.getItem("access_token")}`,
  },
});
```

For production applications, consider a backend-managed secure, HttpOnly cookie rather than browser-accessible token storage.

## Tests

```powershell
npm test
```

## Deployment

### Docker

```powershell
docker build -t frontend-login-api .
docker run --rm -p 8000:8000 `
  -e JWT_SECRET="replace-with-a-private-random-secret-of-32-characters-or-more" `
  -e DEMO_EMAIL="demo@example.com" `
  -e DEMO_PASSWORD="replace-this-password" `
  -e FRONTEND_ORIGINS="https://your-site.example" `
  frontend-login-api
```

On a Node hosting platform, install production dependencies with `npm install --omit=dev` and use `npm start`. Configure the environment variables (`JWT_SECRET`, `DEMO_EMAIL`, `DEMO_PASSWORD`, `FRONTEND_ORIGINS`) as platform secrets/settings. The service listens on `PORT` (default `8000`) and binds to `0.0.0.0`.

## Future database integration

Credential checking and token issuance are isolated in `src/auth.js`. Replace the static credential comparison with a repository/service lookup later while retaining the API contract.
