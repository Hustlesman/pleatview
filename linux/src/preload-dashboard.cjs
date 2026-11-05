// Runs before every page in the dashboard view. Home Assistant's own pages get what the
// Android app gives its WebView: `externalApp`, the frontend's way of asking the host app
// for an access token (so no login page shows), and `pleatviewNative`, which pleatview.js
// reports through. The app answers only Home Assistant's main frame.
const { contextBridge, ipcRenderer } = require("electron");

const parse = (value) => {
  if (typeof value !== "string") return value || {};
  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
};

if (window === window.top) {
  const { externalAuth } = ipcRenderer.sendSync("page", location.origin) || {};
  contextBridge.exposeInMainWorld("pleatviewNative", {
    postMessage: (message) => ipcRenderer.send("bridge", String(message)),
  });
  if (externalAuth) {
    contextBridge.exposeInMainWorld("externalApp", {
      getExternalAuth: (payload) => ipcRenderer.send("bridge", JSON.stringify({ type: "getExternalAuth", payload: parse(payload) })),
      revokeExternalAuth: (payload) => ipcRenderer.send("bridge", JSON.stringify({ type: "revokeExternalAuth", payload: parse(payload) })),
    });
  }
}
