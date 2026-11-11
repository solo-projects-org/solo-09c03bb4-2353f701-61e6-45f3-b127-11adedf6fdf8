"""OAI-PMH 2.0 客户端：只实现本项目需要的 ListRecords(oai_dc)。

- 首次请求携带 from / until / metadataPrefix；
- 续页请求只携带 resumptionToken，替代日期与格式参数；
- 识别 OAI error 码，badResumptionToken / noRecordsMatch 交由上层决策。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


class OAIError(Exception):
    """OAI-PMH error 元素或不可恢复的传输/解析错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class OAIRecord:
    identifier: str
    datestamp: str
    deleted: bool
    dc: dict = field(default_factory=dict)  # Dublin Core 多值字段：标签 -> [值]


@dataclass
class OAIPage:
    records: list[OAIRecord]
    resumption_token: str | None
    complete_list_size: int | None
    cursor: int | None


class OAIClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OAIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, params: dict) -> ET.Element:
        try:
            resp = self._client.get(self.base_url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAIError("transportError", f"OAI 请求失败：{exc}") from exc
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise OAIError("parseError", f"OAI 响应无法解析：{exc}") from exc

        error_el = root.find(f"{OAI_NS}error")
        if error_el is not None:
            code = error_el.get("code", "unknownError")
            raise OAIError(code, (error_el.text or "").strip())
        return root

    def list_records_first(self, *, from_date: str | None,
                           until_date: str) -> OAIPage:
        """首请求：日期区间 + oai_dc，起止日都包含。"""
        params: dict = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
        if from_date:
            params["from"] = from_date
        params["until"] = until_date
        return self._parse_page(self._get(params))

    def list_records_resume(self, resumption_token: str) -> OAIPage:
        """续页请求：只用 resumptionToken，不再带日期和 metadataPrefix。"""
        return self._parse_page(
            self._get({"verb": "ListRecords", "resumptionToken": resumption_token})
        )

    @staticmethod
    def _parse_page(root: ET.Element) -> OAIPage:
        list_el = root.find(f"{OAI_NS}ListRecords")
        if list_el is None:
            raise OAIError("parseError", "响应缺少 ListRecords 元素")

        records: list[OAIRecord] = []
        for rec_el in list_el.findall(f"{OAI_NS}record"):
            header = rec_el.find(f"{OAI_NS}header")
            if header is None:
                raise OAIError("parseError", "record 缺少 header")
            identifier_el = header.find(f"{OAI_NS}identifier")
            datestamp_el = header.find(f"{OAI_NS}datestamp")
            if identifier_el is None or datestamp_el is None:
                raise OAIError("parseError", "header 缺少 identifier/datestamp")

            deleted = header.get("status") == "deleted"
            dc: dict = {}
            metadata = rec_el.find(f"{OAI_NS}metadata")
            if metadata is not None:
                dc_el = next(iter(metadata), None)
                if dc_el is not None:
                    for child in dc_el:
                        tag = child.tag
                        if tag.startswith(DC_NS):
                            label = tag[len(DC_NS):]
                            dc.setdefault(label, []).append(
                                (child.text or "").strip()
                            )
            records.append(
                OAIRecord(
                    identifier=identifier_el.text or "",
                    datestamp=datestamp_el.text or "",
                    deleted=deleted,
                    dc=dc,
                )
            )

        rt_el = list_el.find(f"{OAI_NS}resumptionToken")
        token = None
        complete_size = None
        cursor = None
        if rt_el is not None:
            complete_size = _as_int(rt_el.get("completeListSize"))
            cursor = _as_int(rt_el.get("cursor"))
            if rt_el.text and rt_el.text.strip():
                token = rt_el.text.strip()

        return OAIPage(
            records=records,
            resumption_token=token,
            complete_list_size=complete_size,
            cursor=cursor,
        )


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
