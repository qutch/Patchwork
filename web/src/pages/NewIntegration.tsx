import { Empty } from "../components/ui";

export default function NewIntegration({ draft }: { draft?: string }) {
  return <Empty>The New integration wizard arrives in the next commit{draft ? ` (resuming ${draft})` : ""}.</Empty>;
}
