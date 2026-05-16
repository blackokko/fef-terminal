import { AppShell } from "./components/AppShell";
import { SidecarProvider } from "./context/WebSocketContext";
import "./App.css";

function App() {
  return (
    <SidecarProvider>
      <AppShell />
    </SidecarProvider>
  );
}

export default App;
