import { app } from "./app.js";
import { config } from "./config.js";

app.listen(config.port, "0.0.0.0", () => {
  console.log(`Frontend Login API listening on port ${config.port}`);
});
