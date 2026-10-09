import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./lib/auth";
import { Layout, RequireAuth } from "./components/Layout";
import LoginPage from "./pages/Login";
import RegisterPage from "./pages/Register";
import DashboardPage from "./pages/Dashboard";
import OnboardingPage from "./pages/Onboarding";
import CampaignsPage from "./pages/Campaigns";
import CampaignDetailPage from "./pages/CampaignDetail";
import CalendarPage from "./pages/Calendar";
import ApprovalsPage from "./pages/Approvals";
import AssetsPage from "./pages/Assets";
import AnalyticsPage from "./pages/Analytics";
import AutopilotPage from "./pages/Autopilot";
import OutboxPage from "./pages/Outbox";
import OpsPage from "./pages/Ops";
import InterviewPage from "./pages/Interview";
import BrainPage from "./pages/Brain";
import AffiliatesPage from "./pages/Affiliates";
import DravenPage from "./pages/Draven";
import TemplatesPage from "./pages/Templates";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route path="/" element={<DashboardPage />} />
            <Route path="/onboarding" element={<OnboardingPage />} />
            <Route path="/campaigns" element={<CampaignsPage />} />
            <Route path="/campaigns/:id" element={<CampaignDetailPage />} />
            <Route path="/autopilot" element={<AutopilotPage />} />
            <Route path="/calendar" element={<CalendarPage />} />
            <Route path="/approvals" element={<ApprovalsPage />} />
            <Route path="/assets" element={<AssetsPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/outbox" element={<OutboxPage />} />
            <Route path="/ops" element={<OpsPage />} />
            <Route path="/brain" element={<BrainPage />} />
            <Route path="/interview" element={<InterviewPage />} />
            <Route path="/affiliates" element={<AffiliatesPage />} />
            <Route path="/draven" element={<DravenPage />} />
            <Route path="/templates" element={<TemplatesPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
