import { ChannelJobs } from "@/components/channel-jobs"
import { CollapsibleSection, PageSection } from "@/components/query-state"
import { type Channel, type Source } from "@/lib/api"
import { ArchiveSection } from "@/pages/channel/archive-section"
import { SettingsSection } from "@/pages/channel/settings-section"
import { WordsSection } from "@/pages/channel/words-section"

// The channel page body before this layout: what to archive, words, settings
// with the membership list, and the job lists.
export function DownloadsTab({ channel, sources }: { channel: Channel; sources: Source[] }) {
  return (
    <div className="flex flex-col gap-6">
      <CollapsibleSection title="What to archive" storageKey="channel.archive">
        <ArchiveSection channel={channel} sources={sources} />
      </CollapsibleSection>
      <CollapsibleSection title="Title words" storageKey="channel.words">
        <WordsSection channel={channel} sources={sources} />
      </CollapsibleSection>
      <CollapsibleSection title="Download settings" storageKey="channel.settings">
        <SettingsSection key={channel.id} channel={channel} />
      </CollapsibleSection>
      <PageSection title="Downloads">
        <ChannelJobs channelId={channel.id} />
      </PageSection>
    </div>
  )
}
