from descuentos_anticipados import root_agent


def test_root_agent_arma_receptor_y_emisor():
    assert [a.name for a in root_agent.sub_agents] == ["agente_receptor", "agente_emisor"]
