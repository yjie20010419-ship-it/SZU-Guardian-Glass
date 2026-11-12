import socket
import unittest
from unittest.mock import patch

from szu_guardian.direct_network import (
    AdapterDnsConfig,
    DIRECT_DNS_SERVERS,
    DirectRoute,
    local_ipv4_candidates,
)


class DirectNetworkTests(unittest.TestCase):
    @patch("szu_guardian.direct_network.windows_adapter_dns_configs")
    def test_fake_ip_and_link_local_addresses_are_excluded(
        self,
        windows_adapter_dns_configs,
    ):
        windows_adapter_dns_configs.return_value = []
        with patch.object(
            socket,
            "gethostbyname_ex",
            return_value=(
                "test-pc",
                [],
                ["198.18.0.1", "169.254.1.2", "172.30.8.9", "192.168.1.8"],
            ),
        ):
            result = local_ipv4_candidates()

        self.assertEqual(result, ["172.30.8.9", "192.168.1.8"])

    def test_rewrite_url_uses_direct_dns_result_and_preserves_host(self):
        route = DirectRoute("172.30.8.9")
        route._cache["net.szu.edu.cn"] = ["172.31.63.36"]

        url, host = route.rewrite_url(
            "https://net.szu.edu.cn/cgi-bin/get_challenge?callback=_"
        )

        self.assertEqual(host, "net.szu.edu.cn")
        self.assertEqual(
            url,
            "https://172.31.63.36/cgi-bin/get_challenge?callback=_",
        )

    @patch("szu_guardian.direct_network.query_a_record")
    @patch("szu_guardian.direct_network.windows_adapter_dns_configs")
    def test_discover_prefers_dns_from_the_matching_adapter(
        self,
        adapter_configs,
        query_a_record,
    ):
        adapter_configs.return_value = [
            AdapterDnsConfig(
                source_ip="172.30.230.97",
                dns_servers=("202.96.134.133", "202.96.128.166"),
            )
        ]
        query_a_record.return_value = ["172.31.63.36"]

        route = DirectRoute.discover()

        self.assertEqual(route.source_ip, "172.30.230.97")
        self.assertEqual(
            route.dns_servers,
            ("202.96.134.133", "202.96.128.166", *DIRECT_DNS_SERVERS),
        )
        query_a_record.assert_called_once_with(
            "net.szu.edu.cn",
            "202.96.134.133",
            "172.30.230.97",
        )

    @patch("szu_guardian.direct_network.query_a_record")
    def test_cached_address_is_refreshed_after_expiry(self, query_a_record):
        route = DirectRoute("172.30.8.9", dns_servers=("223.5.5.5",))
        query_a_record.side_effect = [["172.31.63.36"], ["172.31.63.99"]]
        with patch("szu_guardian.direct_network.time.monotonic", return_value=0.0):
            self.assertEqual(route.resolve("net.szu.edu.cn"), ["172.31.63.36"])
        with patch("szu_guardian.direct_network.time.monotonic", return_value=60.0):
            self.assertEqual(route.resolve("net.szu.edu.cn"), ["172.31.63.36"])
        with patch("szu_guardian.direct_network.time.monotonic", return_value=700.0):
            self.assertEqual(route.resolve("net.szu.edu.cn"), ["172.31.63.99"])
        self.assertEqual(query_a_record.call_count, 2)

    @patch("szu_guardian.direct_network.query_a_record")
    def test_failed_refresh_keeps_last_known_address(self, query_a_record):
        route = DirectRoute("172.30.8.9", dns_servers=("223.5.5.5",))
        query_a_record.side_effect = [["172.31.63.36"], OSError("timeout")]
        with patch("szu_guardian.direct_network.time.monotonic", return_value=0.0):
            route.resolve("net.szu.edu.cn")
        with patch("szu_guardian.direct_network.time.monotonic", return_value=700.0):
            self.assertEqual(route.resolve("net.szu.edu.cn"), ["172.31.63.36"])

    @patch("szu_guardian.direct_network.windows_adapter_dns_configs")
    def test_local_candidates_exclude_fake_ip_adapter(self, adapter_configs):
        adapter_configs.return_value = [
            AdapterDnsConfig("172.30.230.97", ("202.96.134.133",)),
        ]

        self.assertEqual(local_ipv4_candidates(), ["172.30.230.97"])


if __name__ == "__main__":
    unittest.main()
