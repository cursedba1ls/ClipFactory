// Thin fetch wrapper: JSON in/out, Russian error messages.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(url, opts);
  } catch (e) {
    throw new ApiError("Нет связи с ClipFactory. Перезапустите программу, если это повторяется.", 0);
  }
  let data = null;
  const text = await resp.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!resp.ok) {
    const msg = (data && data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)))
      || `Ошибка сервера (${resp.status})`;
    throw new ApiError(msg, resp.status);
  }
  return data;
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body ?? {}),
  put: (url, body) => request("PUT", url, body),
  patch: (url, body) => request("PATCH", url, body),
  del: (url) => request("DELETE", url),

  upload(kind, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const url = `/api/media/upload?kind=${encodeURIComponent(kind)}&filename=${encodeURIComponent(file.name)}`;
      xhr.open("POST", url);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new ApiError((data && data.detail) || `Ошибка загрузки (${xhr.status})`, xhr.status));
      };
      xhr.onerror = () => reject(new ApiError("Не удалось загрузить файл: соединение прервано.", 0));
      xhr.send(file);
    });
  },
};
