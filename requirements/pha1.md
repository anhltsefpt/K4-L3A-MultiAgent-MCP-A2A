5. Kiểm tra kết nối MCP Evidence Gateway
Chạy bộ kiểm tra starter và danh sách MCP tools:

pytest -q
day09 --help
day09 mcp-tools
Chép
Pass Signal Pha 1: - pytest -q pass toàn bộ unit tests starter. - day09 mcp-tools xác thực thành công qua COMPETITION_TEAM_API_KEY và in ra danh sách MCP Tools thẩm quyền (get_order, get_payment, get_shipment, get_seller, get_policy,...).



