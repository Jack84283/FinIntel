export type BackendHealth = {
  status: "ok";
  service: "finintel-backend";
  version: string;
};

export function parseBackendHealth(value: unknown): BackendHealth {
  if (value === null || typeof value !== "object") {
    throw new Error("Backend returned an invalid health payload.");
  }
  const obj = value as Record<string, unknown>;
  if (
    obj.status !== "ok" ||
    obj.service !== "finintel-backend" ||
    typeof obj.version !== "string" ||
    obj.version.length === 0
  ) {
    throw new Error("Backend returned an unexpected health response.");
  }
  return { status: "ok", service: "finintel-backend", version: obj.version };
}
