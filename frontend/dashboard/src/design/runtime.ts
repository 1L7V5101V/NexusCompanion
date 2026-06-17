import * as React from "react";
import * as ReactJSXRuntime from "react/jsx-runtime";
import * as ReactDOMClient from "react-dom/client";
import * as UI from "./sdk";

// The shared runtime handed to dynamically-imported plugin modules. The static
// shim files under /assets/sdk/*.js read these off window so that plugins and
// the host resolve react / react-dom / @nexus/dashboard-ui to one instance.
export interface NexusRuntime {
  React: typeof React;
  ReactJSXRuntime: typeof ReactJSXRuntime;
  ReactDOMClient: typeof ReactDOMClient;
  UI: typeof UI;
}

declare global {
  interface Window {
    __nexusRuntime?: NexusRuntime;
  }
}

// Publish the runtime before any plugin is imported.
export function exposeRuntime(): void {
  window.__nexusRuntime = { React, ReactJSXRuntime, ReactDOMClient, UI };
}
