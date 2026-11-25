import { after, before, describe, it } from "node:test";
import assert from "node:assert/strict";

process.env.DEMO_EMAIL = "demo@example.com";
process.env.DEMO_PASSWORD = "ChangeMe123!";
process.env.JWT_SECRET = "test-secret-that-is-long-enough-for-hs256";

const { app } = await import("../src/app.js");
const { default: request } = await import("supertest");

describe("authentication API", () => {
  let server;
  before(() => {
    server = app.listen(0);
  });
  after(() => {
    server.close();
  });

  it("logs in and returns a token accepted by /me", async () => {
    const login = await request(server)
      .post("/api/v1/auth/login")
      .send({ email: "DEMO@example.com", password: "ChangeMe123!" });

    assert.equal(login.status, 200);
    assert.equal(login.body.token_type, "bearer");
    assert.equal(login.body.user.email, "DEMO@example.com");
    assert.equal(login.body.expires_in, 3600);

    const profile = await request(server)
      .get("/api/v1/auth/me")
      .set("Authorization", `Bearer ${login.body.access_token}`);
    assert.equal(profile.status, 200);
    assert.equal(profile.body.email, "DEMO@example.com");
  });

  it("rejects invalid credentials and malformed login requests", async () => {
    const wrongPassword = await request(server)
      .post("/api/v1/auth/login")
      .send({ email: "demo@example.com", password: "wrong" });
    assert.equal(wrongPassword.status, 401);

    const malformed = await request(server)
      .post("/api/v1/auth/login")
      .send({ email: "not-an-email", password: "" });
    assert.equal(malformed.status, 400);
  });

  it("requires a valid bearer token for /me", async () => {
    const response = await request(server)
      .get("/api/v1/auth/me")
      .set("Authorization", "Bearer invalid");
    assert.equal(response.status, 401);
  });

  it("serves Swagger UI and documents both authentication routes", async () => {
    const docs = await request(server).get("/docs/");
    assert.equal(docs.status, 200);
    assert.match(docs.text, /swagger-ui/);

    const specification = await request(server).get("/openapi.json");
    assert.equal(specification.status, 200);
    assert.ok(specification.body.paths["/health"].get);
    assert.ok(specification.body.paths["/auth/login"].post);
    assert.ok(specification.body.paths["/auth/me"].get);
  });

  it("allows configured frontend origins and omits CORS access for other origins", async () => {
    const allowed = await request(server)
      .get("/health")
      .set("Origin", "http://localhost:3000");
    assert.equal(allowed.status, 200);
    assert.equal(allowed.headers["access-control-allow-origin"], "http://localhost:3000");

    const unlisted = await request(server)
      .get("/health")
      .set("Origin", "http://localhost:9000");
    assert.equal(unlisted.status, 200);
    assert.equal(unlisted.headers["access-control-allow-origin"], undefined);
  });
});
