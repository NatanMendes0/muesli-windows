"""Note templates. Builtins mirror common meeting formats; custom ones live in SQLite."""
from . import db

BUILTIN = [
    {
        "id": "auto",
        "name": "Padrão Executivo",
        "builtin": True,
        "description": "Estrutura equilibrada para reuniões em geral.",
        "sections": [
            {"title": "Resumo Executivo", "hint": "Visão geral objetiva de três a cinco frases sobre o objetivo e o andamento da reunião."},
            {"title": "Deliberações e Discussão Técnica", "hint": "Principais pontos discutidos agrupados por tópico com contexto."},
            {"title": "Decisões Tomadas", "hint": "Tudo o que foi acordado ou definido pelos participantes."},
            {"title": "Plano de Ação", "hint": "Tarefas com responsável e prazo/momento acordados."},
        ],
    },
    {
        "id": "one_on_one",
        "name": "1:1 / Alinhamento Individual",
        "builtin": True,
        "description": "Check-in pessoal e alinhamento individual entre duas pessoas.",
        "sections": [
            {"title": "Como as coisas estão", "hint": "Clima, vitórias e atualizações gerais."},
            {"title": "Tópicos Discutidos", "hint": "Principais temas levantados."},
            {"title": "Feedbacks", "hint": "Feedbacks compartilhados em ambas as direções."},
            {"title": "Ações e Compromissos", "hint": "Compromissos firmados com prazos."},
        ],
    },
    {
        "id": "standup",
        "name": "Daily / Standup",
        "builtin": True,
        "description": "Sincronização diária de equipe.",
        "sections": [
            {"title": "Concluído", "hint": "O que foi finalizado desde o último encontro por pessoa."},
            {"title": "Em andamento", "hint": "O que está sendo feito no momento."},
            {"title": "Impedimentos", "hint": "Bloqueios ou necessidades de ajuda."},
            {"title": "Ações do dia", "hint": "Próximos passos combinados na daily."},
        ],
    },
    {
        "id": "weekly_sync",
        "name": "Alinhamento Semanal",
        "builtin": True,
        "description": "Alinhamento semanal de time ou projeto.",
        "sections": [
            {"title": "Resumo da Semana", "hint": "Panorama geral do progresso e prioridades."},
            {"title": "Atualizações por Projeto", "hint": "Status e entregas de cada frente."},
            {"title": "Riscos e Pontos de Atenção", "hint": "Itens atrasados, gargalos ou preocupações."},
            {"title": "Decisões", "hint": "Deliberações e caminhos escolhidos."},
            {"title": "Plano de Ação", "hint": "Tarefas, responsáveis e prazos."},
        ],
    },
    {
        "id": "sales_discovery",
        "name": "Comercial / Descoberta",
        "builtin": True,
        "description": "Reunião de alinhamento com cliente ou prospect.",
        "sections": [
            {"title": "Contexto e Participantes", "hint": "Quem são, cargo e escopo da empresa."},
            {"title": "Dores e Necessidades", "hint": "Problemas descritos e objetivos buscados."},
            {"title": "Cenário Atual", "hint": "Processos, sistemas e ferramentas atuais."},
            {"title": "Orçamento e Cronograma", "hint": "Expectativas de prazo, orçamento e decisão."},
            {"title": "Próximos Passos", "hint": "Ações combinadas de acompanhamento."},
        ],
    },
    {
        "id": "interview",
        "name": "Entrevista",
        "builtin": True,
        "description": "Avaliação e alinhamento de candidatos.",
        "sections": [
            {"title": "Perfil e Trajetória", "hint": "Experiência anterior e pontos fortes citados."},
            {"title": "Perguntas e Respostas Chave", "hint": "Perguntas centrais e como o candidato respondeu."},
            {"title": "Pontos Fortes", "hint": "Destaques positivos."},
            {"title": "Pontos de Atenção", "hint": "Possíveis gaps técnicos ou culturais."},
            {"title": "Próximos Passos", "hint": "Ações no processo seletivo."},
        ],
    },
]


def all_templates():
    return BUILTIN + db.list_custom_templates()


def get(template_id: str):
    for t in all_templates():
        if t["id"] == template_id:
            return t
    return BUILTIN[0]
