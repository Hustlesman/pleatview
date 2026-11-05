// PleatView's own layer talks to the app through this, and nothing else.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pleatview", {
  send: (message) => ipcRenderer.send("ui", message),
  onMessage: (listener) => ipcRenderer.on("ui", (event, message) => listener(message)),
});
