"""Synthetic Workday postings for tests: one layout, a company-specific host per tenant
(`<tenant>.wd<N>.myworkdayjobs.com`). Shaped like the real pages (JSON-LD JobPosting whose hiringOrganization
name some tenants leave empty, a jobPostingHeader, the company only in the description)."""

from __future__ import annotations

import json

from agents.learn import make_page_record

WORKDAY_READERS = {"title": {"jsonld": "title", "jsonld_type": "JobPosting"},
                   "company": {"jsonld": "hiringOrganization.name", "jsonld_type": "JobPosting"}}


def workday_html(title: str, org: str, desc: str) -> str:
    ld = {"@context": "http://schema.org", "@type": "JobPosting", "title": title, "datePosted": "2026-11-10",
          "hiringOrganization": {"@type": "Organization", "name": org},
          "identifier": {"@type": "PropertyValue", "name": title, "value": "R-1"}}
    return (f"<html><head><title>{title}</title><meta property=\"og:title\" content=\"{title}\">"
            f"<meta property=\"og:description\" content=\"{desc}\">"
            f"<script type=\"application/ld+json\">{json.dumps(ld)}</script></head><body>"
            f"<div data-automation-id=\"jobPostingHeader\"><h2>{title}</h2></div>"
            f"<div data-automation-id=\"jobPostingDescription\"><p>{desc}</p></div></body></html>")


TENANTS = {
    "tmobile": ("https://tmobile.wd1.myworkdayjobs.com/External/job/Philadelphia/Software-Engineering-Intern_R1",
                "Software Engineering Intern", "T-Mobile USA, Inc.", "T-Mobile is synonymous with innovation."),
    "nvidia": ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Santa-Clara/Systems-Engineer_R2",
               "Systems Engineer", "NVIDIA", "NVIDIA is the platform upon which every new AI company is built."),
    "visa": ("https://visa.wd5.myworkdayjobs.com/Visa_Early_Careers/job/London/Software-Engineer-Intern_R3",
             "Software Engineer Intern", "", "About Us Visa is a world leader in payments technology."),
}


def workday_page(tenant: str, seq: int) -> dict:
    url, title, org, desc = TENANTS[tenant]
    return make_page_record(url, workday_html(title, org, desc), seq=seq, captured_at="2026-11-14T10:00:00+00:00")
