import { HashRouter, Route, Routes } from "react-router-dom";
import Chrome from "./components/Chrome";
import Screen from "./routes/Screen";
import Asset from "./routes/Asset";
import Rejected from "./routes/Rejected";
import Journal from "./routes/Journal";
import Events from "./routes/Events";
import Health from "./routes/Health";

// HashRouter, NOT BrowserRouter (spec 16.4.2). GitHub Pages has no server-side
// rewrite, so a deep link like /asset/AERO would 404 on a hard refresh under
// BrowserRouter. With the hash, /#/asset/AERO always resolves to index.html and
// the router reads the fragment.

function NotFound() {
  return (
    <div className="max-w-2xl border-l-2 border-line py-8 pl-5">
      <h2 className="text-base font-medium">No such page</h2>
      <p className="mt-2 text-sm text-muted">
        The route does not exist. Links are hash-based, so a URL without a{" "}
        <code className="font-mono">#</code> will land here.
      </p>
    </div>
  );
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route element={<Chrome />}>
          <Route index element={<Screen />} />
          <Route path="asset/:ticker" element={<Asset />} />
          <Route path="rejected" element={<Rejected />} />
          <Route path="journal" element={<Journal />} />
          <Route path="events" element={<Events />} />
          <Route path="health" element={<Health />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
