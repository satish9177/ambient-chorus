import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { Layout } from "./components/shared/Layout";
import { DemoCaseProvider } from "./context/DemoCaseContext";
import { PersonaProvider } from "./context/PersonaContext";
import { AmbientFeedPage } from "./pages/AmbientFeedPage";
import { CaseActionPage } from "./pages/CaseActionPage";
import { MandateThreadPage } from "./pages/MandateThreadPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <PersonaProvider>
        <DemoCaseProvider>
          <BrowserRouter>
            <Layout>
              <Routes>
                <Route path="/" element={<AmbientFeedPage />} />
                <Route path="/mandates/:contributorId" element={<MandateThreadPage />} />
                <Route path="/cases/:caseId" element={<CaseActionPage />} />
              </Routes>
            </Layout>
          </BrowserRouter>
        </DemoCaseProvider>
      </PersonaProvider>
    </QueryClientProvider>
  );
}
