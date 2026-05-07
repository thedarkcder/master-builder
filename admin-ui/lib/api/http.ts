export type Credentials = {
  apiBaseUrl: string;
};

export function parseResponseBody(text: string): unknown {
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

export function stringifyErrorDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        if (item && typeof item === "object") {
          const record = item as { msg?: unknown; loc?: unknown };
          const message = typeof record.msg === "string" ? record.msg : null;
          const location = Array.isArray(record.loc)
            ? record.loc
                .map((part) => String(part))
                .filter(Boolean)
                .join(".")
            : null;
          if (message && location) {
            return `${location}: ${message}`;
          }
          return message;
        }
        return null;
      })
      .filter((value): value is string => Boolean(value));
    if (messages.length > 0) {
      return messages.join("; ");
    }
  }
  if (detail && typeof detail === "object") {
    try {
      return JSON.stringify(detail);
    } catch {
      return "Unexpected error";
    }
  }
  return "Unexpected error";
}

const inFlightGetRequests = new Map<string, Promise<unknown>>();

function requestMethod(init?: RequestInit): string {
  return String(init?.method || "GET").trim().toUpperCase();
}

function requestKey(path: string, init?: RequestInit): string | null {
  if (requestMethod(init) !== "GET" || init?.body || init?.signal) {
    return null;
  }
  return path;
}

async function executeRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`/api/bff${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  const text = await response.text();
  const body = parseResponseBody(text);

  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }

  return body as T;
}

export async function request<T>(
  credentials: Credentials,
  path: string,
  init?: RequestInit,
): Promise<T> {
  void credentials;
  const key = requestKey(path, init);
  if (!key) {
    return executeRequest<T>(path, init);
  }
  const existing = inFlightGetRequests.get(key);
  if (existing) {
    return existing as Promise<T>;
  }
  const pending = executeRequest<T>(path, init).finally(() => {
    inFlightGetRequests.delete(key);
  });
  inFlightGetRequests.set(key, pending);
  return pending;
}

const MAX_NDJSON_STREAM_LINE_BYTES = 1_000_000;

export async function readNdjsonStream<T>(
  response: Response,
  onEvent: (event: T) => void,
  options: {
    signal?: AbortSignal;
    malformedMessage: string;
  },
): Promise<void> {
  if (!response.body) {
    throw new Error("Streaming response did not include a body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancelReader = () => {
    void reader.cancel().catch(() => undefined);
  };
  options.signal?.addEventListener("abort", cancelReader, { once: true });
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > MAX_NDJSON_STREAM_LINE_BYTES) {
        throw new Error(`NDJSON stream line exceeded ${MAX_NDJSON_STREAM_LINE_BYTES} bytes`);
      }
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) {
          try {
            onEvent(JSON.parse(line) as T);
          } catch (error) {
            throw new Error(`${options.malformedMessage}: ${(error as Error).message}`);
          }
        }
        newline = buffer.indexOf("\n");
      }
    }
    buffer += decoder.decode();
    const finalLine = buffer.trim();
    if (finalLine) {
      try {
        onEvent(JSON.parse(finalLine) as T);
      } catch (error) {
        throw new Error(`${options.malformedMessage}: ${(error as Error).message}`);
      }
    }
  } finally {
    options.signal?.removeEventListener("abort", cancelReader);
    reader.releaseLock();
  }
}
