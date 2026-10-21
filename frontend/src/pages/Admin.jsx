import { useEffect, useState, useCallback } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import SystemPanel from "@/components/admin/SystemPanel";
import SelfBotPanel from "@/components/admin/SelfBotPanel";
import GiftCardsPanel from "@/components/admin/GiftCardsPanel";
import UsersPanel from "@/components/admin/UsersPanel";
import BlacklistPanel from "@/components/admin/BlacklistPanel";
import SlotsPanel from "@/components/admin/SlotsPanel";

const Stat = ({ label, value, testid }) => (
  <div className="glass rounded-3xl p-6" data-testid={testid}>
    <div className="label-xs">{label}</div>
    <div className="font-display mt-2 text-3xl font-bold">{value}</div>
  </div>
);

export default function Admin() {
  const [o, setO] = useState(null);
  const { refresh } = useAuth();
  const load = useCallback(() => {
  api.get("/admin/overview").then((r) => setO(r.data));
  refresh();
}, [refresh]);
  useEffect(() => {
  load();
  const t = setInterval(load, 10000);
  return () => clearInterval(t);
}, [load]);
  if (!o) return <div className="text-zinc-500">Loading…</div>;

  return (
    <div className="space-y-8">
      <div className="fade-up">
        <div className="label-xs">Owner panel</div>
        <h1 className="font-display mt-2 text-4xl font-bold sm:text-5xl">Control Room<span className="text-[#FF2A85]">.</span></h1>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Users" value={o.users} testid="admin-stat-users" />
        <Stat label="Active slots" value={o.active_slots} testid="admin-stat-slots" />
        <Stat label="Logs 24h" value={o.logs_24h} testid="admin-stat-logs" />
        <Stat label="Unused gift cards" value={o.giftcards_unused} testid="admin-stat-giftcards" />
      </div>
      <Tabs defaultValue="selfbot">
        <TabsList className="h-auto flex-wrap gap-1 rounded-2xl border border-white/5 bg-white/[0.03] p-1.5">
          {[["selfbot", "Self Bot"], ["system", "System & Bots"], ["gift", "Gift Cards"], ["users", "Users & Whitelist"], ["blacklist", "Brainrot Blacklist"], ["slots", "Slots"]].map(([v, l]) => (
            <TabsTrigger key={v} value={v} data-testid={`admin-tab-${v}`}
              className="rounded-xl px-4 py-2.5 data-[state=active]:bg-[#FF2A85]/15 data-[state=active]:text-white">{l}</TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="selfbot" className="mt-6"><SelfBotPanel o={o} reload={load} /></TabsContent>
        <TabsContent value="system" className="mt-6"><SystemPanel o={o} reload={load} /></TabsContent>
        <TabsContent value="gift" className="mt-6"><GiftCardsPanel reload={load} /></TabsContent>
        <TabsContent value="users" className="mt-6"><UsersPanel /></TabsContent>
        <TabsContent value="blacklist" className="mt-6"><BlacklistPanel o={o} reload={load} /></TabsContent>
        <TabsContent value="slots" className="mt-6"><SlotsPanel /></TabsContent>
      </Tabs>
    </div>
  );
}
