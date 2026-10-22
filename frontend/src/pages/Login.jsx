
import { useEffect, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { api, API, errMsg } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { ClockMark } from "@/components/Logo";

const DiscordIcon = () => (
  <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
    <path d="M20.3 4.4A19.8 19.8 0 0 0 15.4 3l-.6 1.3a18.4 18.4 0 0 0-5.6 0L8.6 3a19.7 19.7 0 0 0-4.9 1.5C.6 9.1-.3 13.6.1 18.1a19.9 19.9 0 0 0 6 3l1.3-2.1a13 13 0 0 1-2-1l.5-.4a14.2 14.2 0 0 0 12.2 0l.5.4c-.6.4-1.3.7-2 1l1.3 2.1a19.8 19.8 0 0 0 6-3c.5-5.2-.9-9.7-3.6-13.7ZM8 15.3c-1.2 0-2.2-1.1-2.2-2.4S6.8 10.5 8 10.5s2.2 1.1 2.2 2.4-1 2.4-2.2 2.4Zm8 0c-1.2 0-2.2-1.1-2.2-2.4s1-2.4 2.2-2.4 2.2 1.1 2.2 2.4-1 2.4-2.2 2.4Z" />
  </svg>
);

export default function Login() {
  const { user } = useAuth();
  const [, setCfg] = useState(null);
  const [params] = useSearchParams();

  useEffect(() => {
    api.get("/auth/config")
      .then((r) => setCfg(r.data))
      .catch(() => {});
    if (params.get("error")) {
      toast.error("Discord login failed, try again");
    }
  }, [params]);

  if (user) return <Navigate to="/" replace />;

  return (
    <div className="relative grid min-h-screen place-items-center px-4">
      <div className="app-bg" />
      <div className="clock-ring" />
      <div className="glass w-full max-w-md rounded-3xl p-10 fade-up" data-testid="login-card">
        <ClockMark size={56} />
        <h1 className="font-display mt-6 text-4xl font-bold sm:text-5xl">
          Horloge<span className="text-[#FF2A85]">.</span>
        </h1>
        <p className="mt-3 text-zinc-400">
          The fastest Steal a Brainrot auto joiner. Every second counts.
        </p>
        <div className="mt-10 space-y-4">
          <a
            href={`${API}/auth/discord/login`}
            data-testid="discord-login-btn"
            className="flex w-full items-center justify-center gap-3 rounded-xl bg-[#5865F2] py-3.5 font-semibold transition-transform duration-200 hover:-translate-y-0.5"
          >
            <DiscordIcon />
            Continue with Discord
          </a>
        </div>
      </div>
    </div>
  );
}
