"""Gráfico de resultados.csv: aciertos, «no sabe» y errores por modo.

    uv run grafico.py      # escribe grafico.png
"""
import matplotlib.pyplot as plt
import pandas as pd

res = pd.read_csv("resultados.csv")
orden_modos = [m for m in ["memoria", "web", "mcp"] if m in set(res.modo)]
tabla = res.groupby("modo").resultado.value_counts().unstack(fill_value=0).reindex(orden_modos)
segundos = res.groupby("modo").segundos.mean().reindex(orden_modos)
categorias = [("acierta", "#0ca30c"), ("no sabe", "#c3c2b7"), ("se equivoca", "#d03b3b")]

fig, ax = plt.subplots(figsize=(8, 3.6), dpi=150)
izquierda = pd.Series(0, index=orden_modos)
for cat, color in categorias:
    n = tabla.get(cat, pd.Series(0, index=orden_modos))
    ax.barh(orden_modos, n, left=izquierda, color=color, height=0.55, edgecolor="white", linewidth=2, label=cat)
    for modo in orden_modos:
        if n[modo]:
            ax.text(izquierda[modo] + n[modo] / 2, modo, str(n[modo]), ha="center", va="center",
                    color="#0b0b0b" if cat == "no sabe" else "white", fontsize=11, fontweight="bold")
    izquierda += n

total = len(res[res.modo == orden_modos[0]])
ax.set_yticks(range(len(orden_modos)), [f"{m}\n({segundos[m]:.0f} s promedio)" for m in orden_modos])
ax.set_xlim(0, total)
ax.set_xlabel(f"Preguntas (de {total})")
ax.invert_yaxis()
for lado in ["top", "right", "left"]:
    ax.spines[lado].set_visible(False)
ax.spines["bottom"].set_color("#c3c2b7")
ax.tick_params(left=False, colors="#52514e")
ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.0), frameon=False)
ax.set_title("¿Acierta Claude (Haiku) las cifras del BCRP?", loc="left", pad=30, fontsize=13)
fig.tight_layout()
fig.savefig("grafico.png", facecolor="white")
